"""Trusted producers for captured Alpaca PAPER preactivation evidence.

This module is the only supported bridge between operational observations and
``captured_paper_readiness_evidence`` v3 probe artifacts.  Callers do not pass
readiness booleans or receipt digests.  They provide bounded authority objects
whose native reads are validated here; this module derives every readiness
field, publishes each canonical artifact with create-new semantics, and then
lets the independent readiness verifier reconstruct and validate the result.

Importing this module performs no broker, provider, database, process, task or
network I/O.  Tests inject inert authorities.  An operational entry point must
construct the concrete authorities only after the dedicated PAPER runtime
environment has been installed.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol, Sequence
import uuid
import xml.etree.ElementTree as ET

from scripts import captured_paper_readiness_evidence as readiness
from scripts import captured_paper_host_cutover as host_cutover
from scripts.captured_paper_runtime_env import (
    CapturedPaperRuntimeEnvironmentReceipt,
    RUNTIME_ENV_SCHEMA_VERSION,
)


UTC = timezone.utc
PROBE_RUN_MANIFEST_SCHEMA_VERSION = (
    "chili.captured-paper-preactivation-probe-run.v1"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_NATIVE_DOCUMENT_BYTES = 16 * 1024 * 1024

# These are fixed by this source file.  An operational executor may not replace
# them with a shorter or caller-selected shard and still mint readiness.
FOCUSED_COMPILE_RELATIVE_PATHS = (
    "app/migrations.py",
    "app/models/captured_paper_selection_frontier.py",
    "app/services/yf_session.py",
    "app/services/trading/momentum_neural/variants.py",
    "scripts/captured_paper_readiness_evidence.py",
    "scripts/iqfeed_capture_only_smoke.py",
    "scripts/run_captured_paper_preactivation_probes.py",
    "scripts/build_captured_paper_runtime_env.py",
    "scripts/build_captured_paper_preactivation.py",
    "scripts/captured_alpaca_paper_service.py",
    "scripts/captured_paper_host_cutover.py",
    "scripts/captured_paper_operator_flow.py",
    "scripts/captured_paper_runtime_env.py",
    "scripts/captured_paper_activation_contract.py",
    "scripts/captured_paper_activation_runner.py",
    "scripts/build_captured_paper_activation_authority.py",
    "scripts/run_captured_paper_operator_chain.py",
    "app/services/trading/momentum_neural/captured_paper_admission.py",
    "app/services/trading/momentum_neural/captured_paper_initial_candidate_reader.py",
    "app/services/trading/momentum_neural/captured_paper_outbox.py",
    "app/services/trading/momentum_neural/captured_paper_selection_producer.py",
    "app/services/trading/momentum_neural/captured_paper_selection_queue.py",
    "app/services/trading/momentum_neural/captured_paper_selection_runtime.py",
    "app/services/trading/momentum_neural/captured_paper_selection_source.py",
    "app/services/trading/momentum_neural/captured_paper_service_supervisor.py",
    "app/services/trading/momentum_neural/captured_paper_transport_coordinator.py",
    "app/services/trading/momentum_neural/captured_paper_variant_binding.py",
)
FOCUSED_PYTEST_NODE_IDS = (
    "tests/test_iqfeed_capture_only_smoke.py::test_capture_only_smoke_binds_real_shape_checks_exact_print_and_quiesces",
    "tests/test_captured_paper_selection_source.py::test_source_captures_full_four_stream_envelope_and_scores_without_fallback",
    "tests/test_captured_paper_selection_source.py::test_missing_typed_fundamentals_receipt_fails_only_that_decision",
    "tests/test_captured_paper_selection_queue.py::test_visible_commit_is_ignored_until_post_fsync_gate_acknowledges_it",
    "tests/test_captured_paper_selection_queue.py::test_coverage_unavailable_event_emits_route_tombstone_not_empty_advance",
    "tests/test_captured_paper_selection_producer.py::test_batch_upsert_and_frontier_cas_commit_together",
    "tests/test_captured_paper_selection_producer.py::test_crash_rollback_then_restart_is_atomic_and_idempotent",
    "tests/test_captured_paper_selection_producer.py::test_migration_353_route_state_schema_and_cas_guards",
    "tests/test_captured_paper_selection_runtime.py::test_constructor_is_fully_inert_then_prime_precedes_reader_install",
    "tests/test_captured_paper_selection_runtime.py::test_hash_bound_not_applied_outcome_never_builds_runtime_or_calls_rollback",
    "tests/test_captured_paper_initial_candidate_reader.py::test_real_db_reader_returns_only_exact_current_captured_row_without_mutation",
    "tests/test_captured_paper_variant_binding.py::test_migration_352_receipt_and_append_only_transition_round_trip",
    "tests/test_captured_paper_variant_binding.py::test_reserved_clone_is_invisible_to_generic_readers_and_mutators",
    "tests/test_captured_paper_service_supervisor.py::test_selection_prime_precedes_fresh_authority_and_order_workers",
    "tests/test_captured_paper_service_supervisor.py::test_post_quiesce_deactivation_runs_after_every_owner_and_before_fence_release",
    "tests/test_captured_alpaca_paper_service.py::test_composition_uses_measured_capacity_and_one_exact_adapter_generation",
    "tests/test_captured_paper_service_selection_integration.py::test_real_service_selection_lifecycle_primes_reads_and_rolls_back",
    "tests/test_yf_session_fundamentals_receipt.py::test_authoritative_empty_is_distinct_from_provider_error",
    "tests/test_yf_session_fundamentals_receipt.py::test_stale_cache_is_not_reclassified_as_fresh_when_circuit_is_open",
    "tests/test_adaptive_risk_policy_settings.py::test_replay_and_captured_paper_use_identical_policy_projection",
    "tests/test_adaptive_risk_policy_settings.py::test_builder_cannot_bind_magic_dollar_or_one_symbol_activation_caps",
    "tests/test_adaptive_risk_policy.py::test_concurrency_emerges_from_aggregate_risk_not_one_symbol_cap",
    "tests/test_adaptive_risk_runtime_contract.py::test_atomic_three_dimension_reservation_and_no_magic_activation_caps_are_required",
    "tests/test_run_captured_paper_operator_chain.py::test_import_is_inert_and_does_not_touch_network_db_broker_or_host",
    "tests/test_run_captured_paper_operator_chain.py::test_chain_request_is_canonical_hash_bound_and_pinned_by_outer_request",
    "tests/test_run_captured_paper_operator_chain.py::test_full_operator_chain_bootstraps_exact_print_before_selection_and_is_hash_bound",
    "tests/test_captured_paper_operator_flow.py::test_operator_flow_publishes_build_ready_and_only_no_order_next_command",
    "tests/test_captured_paper_operator_flow.py::test_materialization_runs_runtime_after_long_shards_and_short_ttl_reads_last",
    "tests/test_captured_paper_runtime_env.py::test_installs_equity_only_candidate_and_excludes_every_live_credential",
    "tests/test_captured_paper_host_cutover.py::test_validate_only_is_default_and_performs_no_mutation",
    "tests/test_build_captured_paper_preactivation.py::test_code_inventory_exactly_matches_activation_contract_and_local_dependency_closure",
    "tests/test_captured_paper_admission.py::test_pre_reservation_breaker_expiring_during_lock_walk_rolls_back_all",
    "tests/test_captured_paper_outbox.py::test_transport_indeterminate_is_reconciliation_only_and_never_terminalized",
    "tests/test_captured_paper_transport_coordinator.py::test_authority_invalidated_after_fence_is_zero_post_and_reconciliation_only",
    "tests/test_captured_paper_fill_watch.py::test_append_commit_must_follow_the_bound_fill_observation",
    "tests/test_captured_paper_activation_contract.py::test_valid_envelope_authorizes_only_fake_money_equity_paper",
    "tests/test_captured_paper_activation_runner.py::test_production_isolation_gate_requires_i_s_b_and_no_site_modules",
    "tests/test_captured_paper_activation_runner.py::test_git_authority_runs_before_secret_install_with_minimal_sanitized_env",
    "tests/test_captured_paper_activation_runner.py::test_validate_only_reaches_real_validate_boundary_but_never_apply",
    "tests/test_captured_paper_activation_runner.py::test_staged_no_order_service_drift_is_rehashed_at_immediate_prelaunch",
    "tests/test_captured_paper_activation_runner.py::test_every_post_apply_failure_runs_exactly_one_exact_rollback",
    "tests/test_captured_paper_activation_runner.py::test_success_result_fsync_failure_remains_inside_compensated_apply_boundary",
    "tests/test_captured_paper_activation_runner.py::test_subprocess_timeout_kills_exact_owned_child_and_grandchild_tree",
    "tests/test_captured_paper_admission.py::test_missing_first_dip_receipt_keeps_daily_opportunity_reusable",
    "tests/test_build_captured_paper_activation_authority.py::test_import_is_inert_stdlib_only_and_performs_no_authority_probe",
    "tests/test_build_captured_paper_activation_authority.py::test_real_temp_git_builds_exact_canonical_loader_roundtrip_and_no_secret_receipt",
    "tests/test_build_captured_paper_activation_authority.py::test_valid_looking_ignored_python_cache_cannot_execute_during_build",
    "tests/test_build_captured_paper_activation_authority.py::test_private_publication_failure_leaves_no_final_or_pending_and_never_overwrites",
    "tests/test_build_captured_paper_activation_authority.py::test_malformed_identity_database_and_secret_configuration_reject",
)
LIFECYCLE_SCENARIOS = (
    "ownership_idempotency",
    "indeterminate_submit_retain",
    "late_fill_quarantine",
    "append_only_fill_settlement",
    "same_cid_reconciliation",
    "no_blind_repost",
)
OPERATIONAL_MAX_AGE_SECONDS_BY_KIND: Mapping[str, int] = MappingProxyType(
    {
        # 2026-07-17: raised from 30s/60s (and then from 5 minutes) — must
        # mirror the contract's _RECEIPT_MAX_AGE_SECONDS table: receipts are
        # issued with expires_at = observed + this ttl and every downstream
        # consumer (finalize, cutover, launcher, ActivatePaper) re-walks the
        # roster against the contract table, so the two tables must agree
        # per kind.  See the contract table for the full sizing rationale.
        "runtime_settings": 10 * 60,
        "broker_account": 10 * 60,
        "database_schema": 10 * 60,
        "capture_host_smoke": 20 * 60,
        "focused_regressions": 60 * 60,
        "lifecycle_preflight": 10 * 60,
        "kill_switch": 10 * 60,
        "rollback_snapshot": 60 * 60,
    }
)


class CapturedPaperPreactivationProbeError(RuntimeError):
    """Stable fail-closed error from a trusted probe producer."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = str(code)
        self.message = str(message)


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CapturedPaperPreactivationProbeError(
            "NON_CANONICAL_VALUE", "probe value is not canonical JSON"
        ) from exc


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha(value: Any, field: str) -> str:
    raw = str(value or "").strip().lower()
    if _SHA256_RE.fullmatch(raw) is None:
        raise CapturedPaperPreactivationProbeError(
            "INVALID_SHA256", f"{field} is not SHA-256"
        )
    return raw


def _utc(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise CapturedPaperPreactivationProbeError(
                "INVALID_TIMESTAMP", f"{field} is not an ISO timestamp"
            ) from exc
    else:
        raise CapturedPaperPreactivationProbeError(
            "INVALID_TIMESTAMP", f"{field} is missing"
        )
    if parsed.tzinfo is None:
        raise CapturedPaperPreactivationProbeError(
            "INVALID_TIMESTAMP", f"{field} is timezone-naive"
        )
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat()


def _exact_keys(value: Mapping[str, Any], expected: set[str], field: str) -> None:
    actual = set(value)
    if actual != expected:
        raise CapturedPaperPreactivationProbeError(
            "SCHEMA_MISMATCH",
            f"{field} keys differ; missing={sorted(expected-actual)} "
            f"extra={sorted(actual-expected)}",
        )


def _strict_json(raw: bytes, field: str) -> Mapping[str, Any]:
    if not raw or len(raw) > _MAX_NATIVE_DOCUMENT_BYTES:
        raise CapturedPaperPreactivationProbeError(
            "DOCUMENT_SIZE_INVALID", f"{field} is empty or over the bounded size"
        )

    def pairs(rows: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in rows:
            if key in result:
                raise CapturedPaperPreactivationProbeError(
                    "DUPLICATE_JSON_KEY", f"{field} repeats {key}"
                )
            result[key] = item
        return result

    def bad_constant(value: str) -> Any:
        raise CapturedPaperPreactivationProbeError(
            "NONFINITE_JSON", f"{field} contains {value}"
        )

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=bad_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CapturedPaperPreactivationProbeError(
            "INVALID_JSON", f"{field} is not strict UTF-8 JSON"
        ) from exc
    if not isinstance(value, Mapping):
        raise CapturedPaperPreactivationProbeError(
            "INVALID_JSON", f"{field} root is not an object"
        )
    return value


def _nonnegative_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CapturedPaperPreactivationProbeError(
            "INVALID_COUNT", f"{field} is not a non-negative integer"
        )
    return value


def _fresh(observed_at: Any, now: datetime, *, seconds: float, field: str) -> datetime:
    observed = _utc(observed_at, field)
    age = (_utc(now, "now") - observed).total_seconds()
    if age < 0 or age > seconds:
        raise CapturedPaperPreactivationProbeError(
            "STALE_AUTHORITY", f"{field} is stale or future-dated"
        )
    return observed


@dataclass(frozen=True, slots=True)
class RuntimeSettingsNativeObservation:
    receipt: CapturedPaperRuntimeEnvironmentReceipt
    settings_projection: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class DatabaseNativeObservation:
    migration_roster: tuple[str, ...]
    applied_migrations: tuple[str, ...]
    table_names: tuple[str, ...]
    rehearsal_case_exit_codes: tuple[int, ...]
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class CaptureHostNativeObservation:
    bootstrap_manifest_sha256: str
    capture_store_root: str
    source_hashes: Mapping[str, str]
    host_binding: Mapping[str, Any]
    capture_health: Mapping[str, Any]
    provider_health: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class CommandExecution:
    argv: tuple[str, ...]
    exit_code: int
    completed_at: datetime
    stdout: bytes = b""
    stderr: bytes = b""


@dataclass(frozen=True, slots=True)
class FocusedRegressionNativeObservation:
    compile_runs: tuple[CommandExecution, ...]
    pytest_run: CommandExecution
    junit_xml: bytes
    side_effect_events: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class LifecycleNativeObservation:
    scenario_run: CommandExecution
    event_report: bytes


@dataclass(frozen=True, slots=True)
class KillSwitchNativeObservation:
    row_id: int
    active: bool
    regime: str
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class RollbackNativeObservation:
    task_snapshot: bytes
    process_snapshot: bytes
    restore_plan: bytes
    candidate_task_xml: bytes
    candidate_action: bytes
    preactivation_baseline: host_cutover.PreActivationRollbackBaseline


class RuntimeSettingsAuthority(Protocol):
    def observe(self) -> RuntimeSettingsNativeObservation: ...


class BrokerReadAuthority(Protocol):
    def adapter(self) -> Any: ...


class DatabaseReadAuthority(Protocol):
    def observe(self) -> DatabaseNativeObservation: ...


class CaptureHostReadAuthority(Protocol):
    def observe(self) -> CaptureHostNativeObservation: ...


class FocusedRegressionAuthority(Protocol):
    def execute(self) -> FocusedRegressionNativeObservation: ...


class LifecycleScenarioAuthority(Protocol):
    def execute(self) -> LifecycleNativeObservation: ...


class KillSwitchReadAuthority(Protocol):
    def observe(self) -> KillSwitchNativeObservation: ...


class RollbackPreactivationBaselineAuthority(Protocol):
    def observe(self) -> RollbackNativeObservation: ...


@dataclass(frozen=True, slots=True)
class TrustedProbeAuthorities:
    runtime_settings: RuntimeSettingsAuthority
    broker_account: BrokerReadAuthority
    database_schema: DatabaseReadAuthority
    capture_host_smoke: CaptureHostReadAuthority
    focused_regressions: FocusedRegressionAuthority
    lifecycle_preflight: LifecycleScenarioAuthority
    kill_switch: KillSwitchReadAuthority
    rollback_snapshot: RollbackPreactivationBaselineAuthority


@dataclass(frozen=True, slots=True)
class TrustedOperationalProbeComposition:
    """In-process authority roster for the single operational probe command.

    The composition is intentionally not deserializable from JSON or CLI
    flags.  The process that owns the installed PAPER runtime, exact adapter,
    live capture host and database engine must construct it.  Rollback input
    validation uses a typed preactivation baseline; final PreparedCutover is
    intentionally unavailable until all preactivation receipts exist.
    This prevents caller-shaped PASS documents from becoming readiness.
    """

    context: readiness.ReadinessValidationContext
    authorities: TrustedProbeAuthorities


@dataclass(frozen=True, slots=True)
class InstalledRuntimeSettingsAuthority:
    """Exact canonical-loader receipt plus parsed Settings projection."""

    receipt: CapturedPaperRuntimeEnvironmentReceipt
    settings_projection: Mapping[str, Any]

    def observe(self) -> RuntimeSettingsNativeObservation:
        return RuntimeSettingsNativeObservation(
            receipt=self.receipt,
            settings_projection=self.settings_projection,
        )


@dataclass(frozen=True, slots=True)
class AlpacaPaperBrokerReadAuthority:
    """Read-only facade over the exact pinned Alpaca PAPER adapter."""

    paper_adapter: Any

    def adapter(self) -> Any:
        return self.paper_adapter


@dataclass(frozen=True, slots=True)
class SqlAlchemyDatabaseReadAuthority:
    """Production schema census with a read-only transaction.

    The optional rehearsal runner is a fixed, separately sandboxed test-DB
    command.  It never runs a migration against the production target.
    """

    engine: Any
    migrations_module: Any
    rehearsal_runner: Callable[[], Sequence[int]]
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def observe(self) -> DatabaseNativeObservation:
        from sqlalchemy import text

        roster = tuple(str(row[0]) for row in self.migrations_module.MIGRATIONS)
        if not roster:
            raise CapturedPaperPreactivationProbeError(
                "DATABASE_MIGRATION_ROSTER_EMPTY", "candidate migration roster is empty"
            )
        try:
            connection = self.engine.connect()
            transaction = connection.begin()
            try:
                if getattr(self.engine.dialect, "name", "") == "postgresql":
                    connection.execute(text("SET TRANSACTION READ ONLY"))
                applied_rows = tuple(
                    str(row[0])
                    for row in connection.execute(
                        text("SELECT version_id FROM schema_version ORDER BY applied_at, version_id")
                    ).fetchall()
                )
                tables = tuple(
                    str(row[0])
                    for row in connection.execute(
                        text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = current_schema() ORDER BY table_name"
                        )
                    ).fetchall()
                )
            finally:
                transaction.rollback()
                connection.close()
        except Exception as exc:
            raise CapturedPaperPreactivationProbeError(
                "DATABASE_READ_UNAVAILABLE", "read-only schema census failed"
            ) from exc
        if len(applied_rows) != len(set(applied_rows)) or set(applied_rows) != set(roster):
            raise CapturedPaperPreactivationProbeError(
                "DATABASE_MIGRATION_DRIFT",
                "applied migration identities differ from the candidate roster",
            )
        applied = tuple(item for item in roster if item in set(applied_rows))
        exit_codes = tuple(self.rehearsal_runner())
        return DatabaseNativeObservation(
            migration_roster=roster,
            applied_migrations=applied,
            table_names=tables,
            rehearsal_case_exit_codes=exit_codes,
            observed_at=_utc(self.wall_clock(), "database wall clock"),
        )


@dataclass(frozen=True, slots=True)
class BoundCaptureHostReadAuthority:
    """Normalize exact bootstrap + running host/capture health receipts."""

    bootstrap_preflight: Any
    host: Any
    capture_health_provider: Callable[[], Mapping[str, Any]]
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def observe(self) -> CaptureHostNativeObservation:
        from scripts.iqfeed_capture_bootstrap_preflight import (
            IqfeedCaptureBootstrapPreflight,
        )

        if type(self.bootstrap_preflight) is not IqfeedCaptureBootstrapPreflight:
            raise CapturedPaperPreactivationProbeError(
                "CAPTURE_PREFLIGHT_INVALID", "bootstrap preflight is not typed"
            )
        if not callable(getattr(self.host, "health", None)):
            raise CapturedPaperPreactivationProbeError(
                "CAPTURE_HOST_UNAVAILABLE", "capture host has no health authority"
            )
        host_health = self.host.health()
        capture_health = self.capture_health_provider()
        supervisor = (
            host_health.get("provider_loop_supervisor")
            if isinstance(host_health, Mapping)
            else None
        )
        if not isinstance(supervisor, Mapping):
            raise CapturedPaperPreactivationProbeError(
                "PROVIDER_HEALTH_UNAVAILABLE", "IQFeed supervisor health is unavailable"
            )
        lanes = supervisor.get("lanes")
        exact_print = bool(
            isinstance(capture_health, Mapping)
            and capture_health.get("exact_print_clock_observed") is True
        )
        provider_health = {
            "observed_at": _iso(_utc(self.wall_clock(), "capture health clock")),
            "socket_readable": bool(
                supervisor.get("all_ready") is True
                and isinstance(lanes, Mapping)
                and set(lanes) == {"trade", "depth"}
                and all(
                    isinstance(row, Mapping)
                    and row.get("socket_connected") is True
                    and row.get("schema_verified") is True
                    for row in lanes.values()
                )
            ),
            "exact_print_clock_observed": exact_print,
        }
        normalized_capture = {
            "capture_store_writable": capture_health.get("capture_store_writable"),
            "dropped_event_count": capture_health.get("dropped_event_count"),
            "overflow_count": capture_health.get("overflow_count"),
            "unreported_gap_count": capture_health.get("unreported_gap_count"),
        }
        return CaptureHostNativeObservation(
            bootstrap_manifest_sha256=self.bootstrap_preflight.manifest_sha256,
            capture_store_root=str(self.bootstrap_preflight.capture_store_root),
            source_hashes=self.bootstrap_preflight.source_hashes,
            host_binding={
                "trade_bridge_bound": host_health.get("trade_bridge_bound"),
                "depth_bridge_bound": host_health.get("depth_bridge_bound"),
            },
            capture_health=normalized_capture,
            provider_health=provider_health,
        )


@dataclass(frozen=True, slots=True)
class CaptureOnlySmokeReadAuthority:
    """Run the isolated capture/provider smoke and expose only typed raw facts.

    Unlike ``BoundCaptureHostReadAuthority``, this authority never needs the
    combined capture+dispatcher host.  The injected runner owns one typed
    capture-only configuration, completes its bounded L1/L2 provider smoke,
    and returns only after both provider threads are stopped and both handoffs
    are unbound.
    """

    smoke_runner: Callable[[], Any]

    def observe(self) -> CaptureHostNativeObservation:
        from scripts.iqfeed_capture_only_smoke import CaptureOnlySmokeEvidence

        if not callable(self.smoke_runner):
            raise CapturedPaperPreactivationProbeError(
                "CAPTURE_SMOKE_UNAVAILABLE", "capture-only smoke runner is unavailable"
            )
        try:
            evidence = self.smoke_runner()
        except Exception as exc:
            raise CapturedPaperPreactivationProbeError(
                "CAPTURE_SMOKE_FAILED", "capture-only provider smoke failed closed"
            ) from exc
        if type(evidence) is not CaptureOnlySmokeEvidence:
            raise CapturedPaperPreactivationProbeError(
                "CAPTURE_SMOKE_INVALID", "capture-only smoke returned shaped evidence"
            )
        closure = evidence.closure
        binding = evidence.host_binding
        if (
            not isinstance(closure, Mapping)
            or not isinstance(binding, Mapping)
            or closure.get("provider_state") != "stopped"
            or closure.get("trade_thread_alive") is not False
            or closure.get("depth_thread_alive") is not False
            or closure.get("bridges_unbound") is not True
            or closure.get("orders_submitted") is not False
            or binding.get("execution_surface") != "capture_only"
            or binding.get("dispatcher_constructed") is not False
            or binding.get("live_runner_loop_constructed") is not False
            or binding.get("broker_adapter_constructed") is not False
            or binding.get("order_transport_constructed") is not False
        ):
            raise CapturedPaperPreactivationProbeError(
                "CAPTURE_SMOKE_NOT_QUIESCENT",
                "capture-only smoke did not prove bounded teardown",
            )
        return CaptureHostNativeObservation(
            bootstrap_manifest_sha256=evidence.bootstrap_manifest_sha256,
            capture_store_root=evidence.capture_store_root,
            source_hashes=evidence.source_hashes,
            host_binding=evidence.host_binding,
            capture_health=evidence.capture_health,
            provider_health=evidence.provider_health,
        )


def _subprocess_environment(
    value: Mapping[str, str] | None,
    *,
    side_effect_report: Path,
) -> dict[str, str]:
    environment = dict(os.environ if value is None else value)
    environment["CHILI_CAPTURED_PAPER_SIDE_EFFECT_REPORT"] = str(
        side_effect_report
    )
    environment["CHILI_PYTEST"] = "1"
    return environment


def _load_side_effect_events(path: Path) -> tuple[Mapping[str, Any], ...]:
    document = _strict_json(path.read_bytes(), "side-effect census")
    body = dict(document)
    claimed = _sha(body.pop("report_sha256", None), "side-effect census")
    events = document.get("events")
    expected_types = (
        "fake_transport",
        "real_network",
        "live_cash",
        "broker_post",
    )
    if (
        document.get("schema_version")
        != "chili.captured-paper-pytest-side-effect-census.v1"
        or readiness.sha256_json(body) != claimed
        or not isinstance(events, list)
        or len(events) != len(expected_types)
    ):
        raise CapturedPaperPreactivationProbeError(
            "SIDE_EFFECT_CENSUS_INVALID", "side-effect census is malformed"
        )
    normalized: list[Mapping[str, Any]] = []
    for index, expected_type in enumerate(expected_types):
        row = events[index]
        if not (
            isinstance(row, Mapping)
            and set(row) == {"event_type", "count"}
            and row.get("event_type") == expected_type
            and type(row.get("count")) is int
            and int(row["count"]) >= 0
        ):
            raise CapturedPaperPreactivationProbeError(
                "SIDE_EFFECT_CENSUS_INVALID",
                "side-effect census roster/counts are not exact",
            )
        normalized.append(dict(row))
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class SubprocessFocusedRegressionAuthority:
    candidate_root: Path
    python_executable: Path = Path(sys.executable)
    environment: Mapping[str, str] | None = None
    command_runner: Callable[..., Any] = subprocess.run
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def execute(self) -> FocusedRegressionNativeObservation:
        compile_rows: list[CommandExecution] = []
        for relative in FOCUSED_COMPILE_RELATIVE_PATHS:
            command = (
                str(self.python_executable),
                "-B",
                "-m",
                "py_compile",
                relative,
            )
            result = self.command_runner(
                command,
                cwd=str(self.candidate_root),
                env=dict(os.environ if self.environment is None else self.environment),
                capture_output=True,
                check=False,
            )
            compile_rows.append(
                CommandExecution(
                    argv=("python", "-B", "-m", "py_compile", relative),
                    exit_code=int(result.returncode),
                    completed_at=_utc(self.wall_clock(), "compile completion clock"),
                    stdout=bytes(result.stdout or b""),
                    stderr=bytes(result.stderr or b""),
                )
            )
        with tempfile.TemporaryDirectory(
            prefix="captured-paper-focused-", dir=str(self.candidate_root)
        ) as raw_temp:
            temporary = Path(raw_temp)
            junit = temporary / "junit.xml"
            side_effect = temporary / "side-effects.json"
            normalized = (
                "python",
                "-B",
                "-m",
                "pytest",
                "-q",
                *FOCUSED_PYTEST_NODE_IDS,
                "-p",
                "scripts.captured_paper_pytest_side_effect_guard",
                "--junitxml=@producer-owned",
            )
            actual = (
                str(self.python_executable),
                *normalized[1:-1],
                f"--junitxml={junit}",
            )
            result = self.command_runner(
                actual,
                cwd=str(self.candidate_root),
                env=_subprocess_environment(
                    self.environment, side_effect_report=side_effect
                ),
                capture_output=True,
                check=False,
            )
            if not junit.is_file() or not side_effect.is_file():
                raise CapturedPaperPreactivationProbeError(
                    "REGRESSION_REPORT_UNAVAILABLE", "fixed pytest reports are missing"
                )
            return FocusedRegressionNativeObservation(
                compile_runs=tuple(compile_rows),
                pytest_run=CommandExecution(
                    argv=normalized,
                    exit_code=int(result.returncode),
                    completed_at=_utc(self.wall_clock(), "pytest completion clock"),
                    stdout=bytes(result.stdout or b""),
                    stderr=bytes(result.stderr or b""),
                ),
                junit_xml=junit.read_bytes(),
                side_effect_events=_load_side_effect_events(side_effect),
            )


@dataclass(frozen=True, slots=True)
class SubprocessLifecycleScenarioAuthority:
    candidate_root: Path
    python_executable: Path = Path(sys.executable)
    environment: Mapping[str, str] | None = None
    command_runner: Callable[..., Any] = subprocess.run
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def execute(self) -> LifecycleNativeObservation:
        from scripts.run_captured_paper_lifecycle_preflight import (
            run_lifecycle_preflight,
        )

        with tempfile.TemporaryDirectory(
            prefix="captured-paper-lifecycle-authority-",
            dir=str(self.candidate_root),
        ) as raw_temp:
            output = Path(raw_temp) / "lifecycle.json"
            run_lifecycle_preflight(
                candidate_root=self.candidate_root,
                output_path=output,
                python_executable=self.python_executable,
                environment=(
                    None if self.environment is None else dict(self.environment)
                ),
                command_runner=self.command_runner,
                wall_clock=self.wall_clock,
            )
            return LifecycleNativeObservation(
                scenario_run=CommandExecution(
                    argv=(
                        "python",
                        "-B",
                        "scripts/run_captured_paper_lifecycle_preflight.py",
                        "--fake-transport-only",
                        "--output=@producer-owned",
                    ),
                    exit_code=0,
                    completed_at=_utc(self.wall_clock(), "lifecycle completion clock"),
                ),
                event_report=output.read_bytes(),
            )


@dataclass(frozen=True, slots=True)
class SqlAlchemyKillSwitchReadAuthority:
    engine: Any
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def observe(self) -> KillSwitchNativeObservation:
        from sqlalchemy import text

        try:
            connection = self.engine.connect()
            transaction = connection.begin()
            try:
                if getattr(self.engine.dialect, "name", "") == "postgresql":
                    connection.execute(text("SET TRANSACTION READ ONLY"))
                row = connection.execute(
                        text(
                            "SELECT id, breaker_tripped, regime FROM trading_risk_state "
                            "WHERE regime = 'kill_switch' "
                            "ORDER BY created_at DESC, id DESC LIMIT 1"
                        )
                    ).fetchone()
            finally:
                transaction.rollback()
                connection.close()
        except Exception as exc:
            raise CapturedPaperPreactivationProbeError(
                "KILL_SWITCH_READ_UNAVAILABLE", "kill-switch query failed"
            ) from exc
        if row is None:
            raise CapturedPaperPreactivationProbeError(
                "KILL_SWITCH_ROW_UNAVAILABLE", "kill-switch state has no durable row"
            )
        return KillSwitchNativeObservation(
            row_id=row[0],
            active=row[1],
            regime=str(row[2] or ""),
            observed_at=_utc(self.wall_clock(), "kill-switch clock"),
        )


@dataclass(frozen=True, slots=True)
class HostCutoverPreactivationBaselineAuthority:
    """Validate immutable rollback inputs without claiming final ValidateOnly."""

    context: readiness.ReadinessValidationContext
    candidate_root: Path
    allowed_read_roots: tuple[Path, ...]
    task_snapshot_path: Path
    process_snapshot_path: Path
    restore_plan_path: Path
    candidate_task_xml_path: Path
    candidate_action_path: Path
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def observe(self) -> RollbackNativeObservation:
        paths = (
            self.task_snapshot_path,
            self.process_snapshot_path,
            self.restore_plan_path,
            self.candidate_task_xml_path,
            self.candidate_action_path,
        )
        if any(not path.is_absolute() or not path.is_file() for path in paths):
            raise CapturedPaperPreactivationProbeError(
                "ROLLBACK_ARTIFACT_UNAVAILABLE",
                "preactivation rollback artifact is unavailable",
            )
        rows = tuple(path.read_bytes() for path in paths)
        if any(not raw or len(raw) > _MAX_NATIVE_DOCUMENT_BYTES for raw in rows):
            raise CapturedPaperPreactivationProbeError(
                "ROLLBACK_ARTIFACT_INVALID",
                "preactivation rollback artifact is empty/oversized",
            )
        try:
            baseline = host_cutover.prepare_preactivation_rollback_baseline(
                host_cutover.PreActivationRollbackContext(
                    activation_generation=self.context.activation_generation,
                    expected_account_id=self.context.expected_account_id,
                    candidate_root=self.candidate_root,
                    allowed_read_roots=self.allowed_read_roots,
                    host_cutover_source_sha256=_sha(
                        self.context.source_hashes.get(
                            "captured_paper_host_cutover"
                        ),
                        "host cutover source",
                    ),
                    launcher_argument_contract_sha256=(
                        self.context.launcher_argument_contract_sha256
                    ),
                ),
                task_snapshot_path=self.task_snapshot_path,
                process_snapshot_path=self.process_snapshot_path,
                restore_plan_path=self.restore_plan_path,
                candidate_task_template_path=self.candidate_task_xml_path,
                candidate_action_path=self.candidate_action_path,
                validated_at=_utc(self.wall_clock(), "rollback baseline clock"),
            )
        except host_cutover.CapturedPaperHostCutoverError as exc:
            raise CapturedPaperPreactivationProbeError(
                "ROLLBACK_PREACTIVATION_BASELINE_INVALID", exc.code
            ) from exc
        expected_hashes = (
            baseline.task_snapshot.artifact_sha256,
            baseline.process_snapshot.artifact_sha256,
            baseline.restore_plan.artifact_sha256,
            baseline.candidate_template_sha256,
            baseline.candidate_action_sha256,
        )
        if any(
            _sha256_bytes(raw) != expected
            for raw, expected in zip(rows, expected_hashes, strict=True)
        ):
            raise CapturedPaperPreactivationProbeError(
                "ROLLBACK_ARTIFACT_DRIFT",
                "preactivation rollback artifacts changed after validation",
            )
        return RollbackNativeObservation(
            task_snapshot=rows[0],
            process_snapshot=rows[1],
            restore_plan=rows[2],
            candidate_task_xml=rows[3],
            candidate_action=rows[4],
            preactivation_baseline=baseline,
        )


def _runtime_observations(
    authority: RuntimeSettingsAuthority,
    *,
    context: readiness.ReadinessValidationContext,
    now: datetime,
) -> Mapping[str, Any]:
    del now
    native = authority.observe()
    if type(native) is not RuntimeSettingsNativeObservation:
        raise CapturedPaperPreactivationProbeError(
            "RUNTIME_AUTHORITY_INVALID", "runtime authority returned a shaped PASS object"
        )
    receipt = native.receipt
    if type(receipt) is not CapturedPaperRuntimeEnvironmentReceipt:
        raise CapturedPaperPreactivationProbeError(
            "RUNTIME_AUTHORITY_INVALID", "runtime receipt is not canonical loader output"
        )
    receipt_body = {
        "schema_version": RUNTIME_ENV_SCHEMA_VERSION,
        "source_path": receipt.source_path,
        "source_sha256": receipt.source_sha256,
        "expected_account_id": receipt.expected_account_id,
        "first_dip_policy_mode": receipt.first_dip_policy_mode,
        "effective_config": dict(receipt.effective_config),
        "secret_fingerprints": dict(receipt.secret_fingerprints),
    }
    if (
        receipt.schema_version != RUNTIME_ENV_SCHEMA_VERSION
        or receipt.expected_account_id != context.expected_account_id
        or readiness.sha256_json(receipt_body) != receipt.configuration_sha256
        or receipt.configuration_sha256 != context.runtime_environment_sha256
    ):
        raise CapturedPaperPreactivationProbeError(
            "RUNTIME_BINDING_MISMATCH", "runtime receipt is not context-bound"
        )
    projection = dict(native.settings_projection)
    claimed_projection = _sha(
        projection.get("settings_projection_sha256"), "settings projection"
    )
    unsigned = dict(projection)
    unsigned.pop("settings_projection_sha256", None)
    settings = projection.get("settings")
    policy = projection.get("adaptive_risk_policy")
    if (
        readiness.sha256_json(unsigned) != claimed_projection
        or claimed_projection != context.effective_config_sha256
        or projection.get("runtime_environment_sha256")
        != receipt.configuration_sha256
        or not isinstance(settings, Mapping)
        or not isinstance(policy, Mapping)
    ):
        raise CapturedPaperPreactivationProbeError(
            "SETTINGS_BINDING_MISMATCH", "settings projection is not canonical"
        )
    policy_body = dict(policy)
    policy_projection_sha = _sha(
        policy_body.pop("settings_projection_sha256", None),
        "adaptive policy settings projection",
    )
    if readiness.sha256_json(policy_body) != policy_projection_sha:
        raise CapturedPaperPreactivationProbeError(
            "POLICY_BINDING_MISMATCH", "adaptive policy projection digest mismatched"
        )
    policy_sha = _sha(policy.get("policy_sha256"), "adaptive policy")
    activation_dollar_caps = sorted(
        key
        for key in policy.get("settings", {})
        if ("activation" in str(key).lower() or "paper" in str(key).lower())
        and ("usd" in str(key).lower() or "dollar" in str(key).lower())
    )
    activation_symbol_caps = sorted(
        key
        for key in policy.get("settings", {})
        if ("activation" in str(key).lower() or "paper" in str(key).lower())
        and "symbol" in str(key).lower()
    )
    return MappingProxyType(
        {
            "runtime_environment_sha256": receipt.configuration_sha256,
            "settings_projection_sha256": claimed_projection,
            "execution_broker": "alpaca" if settings.get("chili_alpaca_enabled") is True else "disabled",
            "broker_environment": "paper" if settings.get("chili_alpaca_paper") is True else "live",
            "execution_rail": settings.get("chili_equity_execution_rail"),
            "paper_credentials_present": projection.get("paper_credentials_present"),
            "live_cash_credentials_present": projection.get("live_cash_credentials_present"),
            "cash_broker_environment_keys_present": projection.get(
                "cash_broker_environment_keys_present"
            ),
            "equity_only": settings.get("chili_momentum_auto_arm_equity_only"),
            "short_authorized": bool(
                settings.get("chili_momentum_short_enabled")
                or settings.get("chili_momentum_short_lane_enabled")
            ),
            "crypto_authorized": settings.get(
                "chili_momentum_auto_arm_crypto_only"
            ),
            "first_dip_policy_mode": settings.get(
                "chili_momentum_first_dip_reclaim_policy_mode"
            ),
            "adaptive_policy_sha256": policy_sha,
            "policy_surfaces": ["captured_paper", "replay_v3"],
            "activation_only_dollar_caps": activation_dollar_caps,
            "activation_only_symbol_caps": activation_symbol_caps,
        }
    )


def _validate_audit_snapshot(
    value: Any,
    *,
    account_id: str,
    generation: str,
) -> tuple[int, str]:
    if not isinstance(value, Mapping):
        raise CapturedPaperPreactivationProbeError(
            "BROKER_AUDIT_UNAVAILABLE", "order submission audit is unavailable"
        )
    canonical = str(value.get("snapshot_canonical_json") or "")
    claimed = _sha(value.get("snapshot_sha256"), "order submission audit")
    count = _nonnegative_int(value.get("submission_call_count"), "submission count")
    if (
        value.get("schema_version")
        != "chili.alpaca-paper-order-submission-audit.v1"
        or value.get("broker_environment") != "paper"
        or value.get("asset_class") != "us_equity"
        or value.get("provider_account_id") != account_id
        or value.get("adapter_connection_generation") != generation
        or _sha256_bytes(canonical.encode("utf-8")) != claimed
    ):
        raise CapturedPaperPreactivationProbeError(
            "BROKER_AUDIT_MISMATCH", "order submission audit is malformed"
        )
    return count, claimed


def _broker_observations(
    authority: BrokerReadAuthority,
    *,
    context: readiness.ReadinessValidationContext,
    now: datetime,
) -> Mapping[str, Any]:
    adapter = authority.adapter()
    required = (
        "get_paper_connection_generation_receipt",
        "get_order_submission_audit_snapshot",
        "get_account_snapshot",
        "get_paper_position_census",
        "get_paper_open_order_census",
    )
    if any(not callable(getattr(adapter, name, None)) for name in required):
        raise CapturedPaperPreactivationProbeError(
            "BROKER_AUTHORITY_INVALID", "broker authority is not exact read-only Alpaca PAPER"
        )
    connection = adapter.get_paper_connection_generation_receipt()
    if not isinstance(connection, Mapping):
        raise CapturedPaperPreactivationProbeError(
            "BROKER_CONNECTION_UNAVAILABLE", "connection receipt is unavailable"
        )
    generation = str(connection.get("adapter_connection_generation") or "")
    canonical = str(connection.get("receipt_canonical_json") or "")
    connection_sha = _sha(connection.get("receipt_sha256"), "connection receipt")
    if (
        connection.get("schema_version")
        != "chili.alpaca-paper-connection-generation.v1"
        or connection.get("broker_environment") != "paper"
        or connection.get("asset_class") != "us_equity"
        or connection.get("provider_account_id") != context.expected_account_id
        or not generation.startswith("alpaca-paper-rest:")
        or _SHA256_RE.fullmatch(generation.split(":", 1)[1]) is None
        or _sha256_bytes(canonical.encode("utf-8")) != connection_sha
    ):
        raise CapturedPaperPreactivationProbeError(
            "BROKER_CONNECTION_MISMATCH", "connection is not exact Alpaca PAPER"
        )
    _fresh(connection.get("available_at"), now, seconds=10, field="connection.available_at")
    audit_before = adapter.get_order_submission_audit_snapshot()
    before_count, before_sha = _validate_audit_snapshot(
        audit_before,
        account_id=context.expected_account_id,
        generation=generation,
    )
    account = adapter.get_account_snapshot()
    if not isinstance(account, Mapping) or account.get("ok") is not True:
        raise CapturedPaperPreactivationProbeError(
            "BROKER_ACCOUNT_UNAVAILABLE", "account read failed"
        )
    account_observed = _fresh(
        account.get("retrieved_at_utc"), now, seconds=10, field="account.retrieved_at"
    )
    binding = {
        "schema_version": "chili.captured-paper-preactivation-broker-read.v1",
        "activation_generation": context.activation_generation,
        "account_scope": "alpaca:paper",
        "expected_account_id": context.expected_account_id,
        "connection_receipt_sha256": connection_sha,
        "orders_submitted_before": before_count,
    }
    positions = adapter.get_paper_position_census(read_binding=binding)
    orders = adapter.get_paper_open_order_census(read_binding=binding)
    audit_after = adapter.get_order_submission_audit_snapshot()
    after_count, after_sha = _validate_audit_snapshot(
        audit_after,
        account_id=context.expected_account_id,
        generation=generation,
    )
    if before_count != after_count or before_sha != after_sha:
        raise CapturedPaperPreactivationProbeError(
            "BROKER_READ_SIDE_EFFECT", "read-only broker probe advanced POST audit"
        )
    for name, census, schema, rows_key in (
        (
            "position",
            positions,
            "chili.alpaca-paper-position-census.v1",
            "positions",
        ),
        (
            "order",
            orders,
            "chili.alpaca-paper-open-order-census.v1",
            "orders",
        ),
    ):
        if not (
            isinstance(census, Mapping)
            and census.get("readable") is True
            and census.get("pagination_complete") is True
            and census.get("broker_environment") == "paper"
            and census.get("asset_class") == "us_equity"
            and census.get("provider_account_id") == context.expected_account_id
            and census.get("adapter_connection_generation") == generation
            and isinstance(census.get(rows_key), list)
            and census.get(rows_key) == []
            and _sha(census.get("inventory_sha256"), f"{name} inventory")
            == _sha256_bytes(b"[]")
        ):
            raise CapturedPaperPreactivationProbeError(
                "BROKER_NOT_FLAT", f"{name} census is incomplete or non-empty"
            )
    blocked = (
        "account_blocked",
        "trading_blocked",
        "transfers_blocked",
        "trade_suspended_by_user",
    )
    if (
        account.get("paper") is not True
        or account.get("account_id") != context.expected_account_id
        or str(account.get("status") or "").upper() != "ACTIVE"
        or any(account.get(name) is not False for name in blocked)
    ):
        raise CapturedPaperPreactivationProbeError(
            "BROKER_ACCOUNT_UNSAFE", "account posture is not active unblocked PAPER"
        )
    return MappingProxyType(
        {
            "account_identity_sha256": readiness.sha256_json(
                {
                    "account_id": context.expected_account_id,
                    "broker": "alpaca",
                    "environment": "paper",
                }
            ),
            "connection_generation": generation,
            "connection_receipt_sha256": connection_sha,
            "account_status": "ACTIVE",
            "account_blocked": False,
            "trading_blocked": False,
            "transfers_blocked": False,
            "trade_suspended_by_user": False,
            "position_count": 0,
            "open_order_count": 0,
            "position_inventory_sha256": _sha256_bytes(b"[]"),
            "open_order_inventory_sha256": _sha256_bytes(b"[]"),
            "observed_at": _iso(account_observed),
            "paper_execution_only": True,
        }
    )


def _database_observations(
    authority: DatabaseReadAuthority,
    *,
    context: readiness.ReadinessValidationContext,
    now: datetime,
) -> Mapping[str, Any]:
    native = authority.observe()
    if type(native) is not DatabaseNativeObservation:
        raise CapturedPaperPreactivationProbeError(
            "DATABASE_AUTHORITY_INVALID", "database authority returned shaped evidence"
        )
    roster = tuple(native.migration_roster)
    applied = tuple(native.applied_migrations)
    if (
        not roster
        or len(roster) != len(set(roster))
        or tuple(applied) != roster
        or set(readiness.REQUIRED_DATABASE_TABLES) - set(native.table_names)
    ):
        raise CapturedPaperPreactivationProbeError(
            "DATABASE_SCHEMA_INCOMPLETE", "migration/table roster is incomplete"
        )
    if not native.rehearsal_case_exit_codes or any(
        not isinstance(code, int) or isinstance(code, bool)
        for code in native.rehearsal_case_exit_codes
    ):
        raise CapturedPaperPreactivationProbeError(
            "DATABASE_REHEARSAL_INVALID", "fixed rehearsal did not return exact exit codes"
        )
    observed = _fresh(native.observed_at, now, seconds=300, field="database.observed_at")
    roster_sha = readiness.sha256_json(list(roster))
    applied_sha = readiness.sha256_json(list(applied))
    return MappingProxyType(
        {
            "database_target_fingerprint": context.database_target_fingerprint,
            "migration_roster_sha256": roster_sha,
            "applied_migrations_sha256": applied_sha,
            "latest_migration": roster[-1],
            "migration_count": len(roster),
            "required_tables": list(readiness.REQUIRED_DATABASE_TABLES),
            "idempotent_rehearsal_pass_count": sum(
                code == 0 for code in native.rehearsal_case_exit_codes
            ),
            "idempotent_rehearsal_failure_count": sum(
                code != 0 for code in native.rehearsal_case_exit_codes
            ),
            "observed_at": _iso(observed),
        }
    )


def _capture_observations(
    authority: CaptureHostReadAuthority,
    *,
    context: readiness.ReadinessValidationContext,
    now: datetime,
) -> Mapping[str, Any]:
    native = authority.observe()
    if type(native) is not CaptureHostNativeObservation:
        raise CapturedPaperPreactivationProbeError(
            "CAPTURE_AUTHORITY_INVALID", "capture authority returned shaped evidence"
        )
    required_roles = (
        "iqfeed_capture_host",
        "iqfeed_trade_bridge",
        "iqfeed_depth_bridge",
        "iqfeed_l1_capture",
        "iqfeed_l2_capture",
    )
    hashes = {
        role: _sha(native.source_hashes.get(role), f"capture source {role}")
        for role in required_roles
    }
    binding = native.host_binding
    health = native.capture_health
    provider = native.provider_health
    if (
        _sha(native.bootstrap_manifest_sha256, "bootstrap manifest")
        != context.iqfeed_bootstrap_manifest_sha256
        or str(Path(native.capture_store_root)) != context.capture_store_root
        or any(hashes[role] != context.source_hashes.get(role) for role in required_roles)
        or not isinstance(binding, Mapping)
        or binding.get("trade_bridge_bound") is not True
        or binding.get("depth_bridge_bound") is not True
        or not isinstance(health, Mapping)
        or not isinstance(provider, Mapping)
    ):
        raise CapturedPaperPreactivationProbeError(
            "CAPTURE_BINDING_MISMATCH", "capture host is not bound to candidate sources"
        )
    dropped = _nonnegative_int(health.get("dropped_event_count"), "capture dropped")
    overflow = _nonnegative_int(health.get("overflow_count"), "capture overflow")
    gaps = _nonnegative_int(health.get("unreported_gap_count"), "capture gaps")
    provider_observed = _fresh(
        provider.get("observed_at"), now, seconds=60, field="provider health"
    )
    from scripts.iqfeed_capture_only_smoke import (
        equity_extended_session_is_open,
    )

    closed_session_activation_only = bool(
        provider.get("activation_only_closed_session_without_exact_print") is True
        and not equity_extended_session_is_open(now)
    )
    if (
        health.get("capture_store_writable") is not True
        or provider.get("socket_readable") is not True
        or (
            provider.get("exact_print_clock_observed") is not True
            and not closed_session_activation_only
        )
    ):
        raise CapturedPaperPreactivationProbeError(
            "CAPTURE_HEALTH_UNAVAILABLE", "capture/provider health is not executable"
        )
    return MappingProxyType(
        {
            "iqfeed_bootstrap_manifest_sha256": context.iqfeed_bootstrap_manifest_sha256,
            "capture_store_root": context.capture_store_root,
            "source_hashes": hashes,
            "l1_bound": True,
            "l2_policy": "decision_local_fail_closed",
            "capture_store_writable": True,
            "dropped_event_count": dropped,
            "overflow_count": overflow,
            "unreported_gap_count": gaps,
            "provider_health_observed_at": _iso(provider_observed),
        }
    )


def _parse_junit(raw: bytes) -> tuple[int, int, int, int, tuple[str, ...]]:
    if not raw or len(raw) > _MAX_NATIVE_DOCUMENT_BYTES:
        raise CapturedPaperPreactivationProbeError(
            "JUNIT_INVALID", "JUnit report is empty or over the bounded size"
        )
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise CapturedPaperPreactivationProbeError(
            "JUNIT_INVALID", "JUnit report is not XML"
        ) from exc
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    if not suites:
        suites = list(root.iter("testsuite"))
    tests = failures = errors = skipped = 0
    for suite in suites:
        tests += int(suite.attrib.get("tests", "0"))
        failures += int(suite.attrib.get("failures", "0"))
        errors += int(suite.attrib.get("errors", "0"))
        skipped += int(suite.attrib.get("skipped", "0"))
    case_names = tuple(str(case.attrib.get("name") or "") for case in root.iter("testcase"))
    if tests <= 0 or skipped or len(case_names) != tests or any(not name for name in case_names):
        raise CapturedPaperPreactivationProbeError(
            "JUNIT_INCOMPLETE", "focused regression report is empty or skipped"
        )
    return tests, failures, errors, skipped, case_names


def _focused_regression_observations(
    authority: FocusedRegressionAuthority,
    *,
    context: readiness.ReadinessValidationContext,
    now: datetime,
) -> Mapping[str, Any]:
    native = authority.execute()
    if type(native) is not FocusedRegressionNativeObservation:
        raise CapturedPaperPreactivationProbeError(
            "REGRESSION_AUTHORITY_INVALID", "regression authority returned shaped counts"
        )
    expected_compile = tuple(
        ("python", "-B", "-m", "py_compile", relative)
        for relative in FOCUSED_COMPILE_RELATIVE_PATHS
    )
    if tuple(run.argv for run in native.compile_runs) != expected_compile:
        raise CapturedPaperPreactivationProbeError(
            "REGRESSION_COMMAND_MISMATCH", "compile command roster is not fixed"
        )
    expected_pytest = (
        "python",
        "-B",
        "-m",
        "pytest",
        "-q",
        *FOCUSED_PYTEST_NODE_IDS,
        "-p",
        "scripts.captured_paper_pytest_side_effect_guard",
        "--junitxml=@producer-owned",
    )
    if native.pytest_run.argv != expected_pytest:
        raise CapturedPaperPreactivationProbeError(
            "REGRESSION_COMMAND_MISMATCH", "pytest command is not the fixed shard"
        )
    if native.pytest_run.exit_code != 0:
        raise CapturedPaperPreactivationProbeError(
            "REGRESSION_COMMAND_FAILED", "fixed pytest shard failed"
        )
    selected, failures, errors, _skipped, case_names = _parse_junit(native.junit_xml)
    expected_case_names = tuple(node.rsplit("::", 1)[1] for node in FOCUSED_PYTEST_NODE_IDS)
    observed_case_names = tuple(name.split("[", 1)[0] for name in case_names)
    if (
        len(set(expected_case_names)) != len(expected_case_names)
        or set(observed_case_names) != set(expected_case_names)
        or any(observed_case_names.count(name) < 1 for name in expected_case_names)
    ):
        raise CapturedPaperPreactivationProbeError(
            "REGRESSION_TEST_ROSTER_MISMATCH",
            "JUnit testcases differ from the fixed focused shard",
        )
    events = tuple(native.side_effect_events)
    allowed_types = {"fake_transport", "real_network", "live_cash", "broker_post"}
    if any(
        not isinstance(event, Mapping)
        or set(event) != {"event_type", "count"}
        or event.get("event_type") not in allowed_types
        or not isinstance(event.get("count"), int)
        or isinstance(event.get("count"), bool)
        or event.get("count") < 0
        for event in events
    ):
        raise CapturedPaperPreactivationProbeError(
            "SIDE_EFFECT_REPORT_INVALID", "side-effect events are malformed"
        )
    counts = {name: 0 for name in allowed_types}
    for event in events:
        counts[str(event["event_type"])] += int(event["count"])
    completed = _fresh(
        native.pytest_run.completed_at,
        now,
        seconds=3600,
        field="focused regressions completed_at",
    )
    return MappingProxyType(
        {
            "code_build_sha256": context.code_build_sha256,
            "compile_file_count": len(native.compile_runs),
            "compile_failure_count": sum(run.exit_code != 0 for run in native.compile_runs),
            "selected_test_count": selected,
            "passed_test_count": selected - failures - errors,
            "failed_test_count": failures,
            "error_test_count": errors,
            "real_network_call_count": counts["real_network"],
            "live_cash_call_count": counts["live_cash"],
            "real_broker_post_call_count": counts["broker_post"],
            "completed_at": _iso(completed),
        }
    )


def _lifecycle_observations(
    authority: LifecycleScenarioAuthority,
    *,
    context: readiness.ReadinessValidationContext,
    now: datetime,
) -> Mapping[str, Any]:
    del context
    native = authority.execute()
    if type(native) is not LifecycleNativeObservation:
        raise CapturedPaperPreactivationProbeError(
            "LIFECYCLE_AUTHORITY_INVALID", "lifecycle authority returned shaped booleans"
        )
    expected_argv = (
        "python",
        "-B",
        "scripts/run_captured_paper_lifecycle_preflight.py",
        "--fake-transport-only",
        "--output=@producer-owned",
    )
    if native.scenario_run.argv != expected_argv or native.scenario_run.exit_code != 0:
        raise CapturedPaperPreactivationProbeError(
            "LIFECYCLE_COMMAND_MISMATCH", "lifecycle executable was not the fixed harness"
        )
    report = _strict_json(native.event_report, "lifecycle event report")
    _exact_keys(
        report,
        {"schema_version", "scenarios", "transport_events", "completed_at"},
        "lifecycle event report",
    )
    scenarios = report.get("scenarios")
    transport_events = report.get("transport_events")
    if not isinstance(scenarios, list) or not isinstance(transport_events, list):
        raise CapturedPaperPreactivationProbeError(
            "LIFECYCLE_REPORT_INVALID", "lifecycle scenarios/events are unavailable"
        )
    by_name: dict[str, Mapping[str, Any]] = {}
    for row in scenarios:
        if not isinstance(row, Mapping) or set(row) != {"name", "events"}:
            raise CapturedPaperPreactivationProbeError(
                "LIFECYCLE_REPORT_INVALID", "lifecycle scenario is malformed"
            )
        name = str(row.get("name") or "")
        events = row.get("events")
        if name in by_name or not isinstance(events, list) or not events:
            raise CapturedPaperPreactivationProbeError(
                "LIFECYCLE_REPORT_INVALID", "lifecycle scenario identity is ambiguous"
            )
        by_name[name] = row
    if tuple(sorted(by_name)) != tuple(sorted(LIFECYCLE_SCENARIOS)):
        raise CapturedPaperPreactivationProbeError(
            "LIFECYCLE_SCENARIO_MISMATCH", "lifecycle scenario roster is not exact"
        )
    required_events = {
        "ownership_idempotency": {"claim_acquired", "duplicate_claim_refused"},
        "indeterminate_submit_retain": {"submit_indeterminate", "resources_retained"},
        "late_fill_quarantine": {"late_fill_observed", "exposure_quarantined"},
        "append_only_fill_settlement": {"fill_appended", "settlement_appended"},
        "same_cid_reconciliation": {"same_cid_lookup", "same_cid_reconciled"},
        "no_blind_repost": {"indeterminate_observed", "reconciliation_only"},
    }
    for name, needed in required_events.items():
        actual = {str(item) for item in by_name[name]["events"]}
        if not needed <= actual:
            raise CapturedPaperPreactivationProbeError(
                "LIFECYCLE_SCENARIO_FAILED", f"{name} lacks required executable events"
            )
    transport_counts = {"fake_post": 0, "real_network": 0, "live_cash": 0, "blind_repost": 0}
    for event in transport_events:
        if not isinstance(event, Mapping) or set(event) != {"event_type", "count"}:
            raise CapturedPaperPreactivationProbeError(
                "LIFECYCLE_REPORT_INVALID", "transport event is malformed"
            )
        event_type = str(event.get("event_type") or "")
        if event_type not in transport_counts:
            raise CapturedPaperPreactivationProbeError(
                "LIFECYCLE_REPORT_INVALID", "transport event type is unsupported"
            )
        transport_counts[event_type] += _nonnegative_int(event.get("count"), event_type)
    completed = _fresh(
        report.get("completed_at"), now, seconds=300, field="lifecycle completed_at"
    )
    return MappingProxyType(
        {
            "runtime_scenario_count": len(by_name),
            "passed_scenario_count": len(by_name),
            "failed_scenario_count": 0,
            "fake_transport_call_count": transport_counts["fake_post"],
            "real_network_call_count": transport_counts["real_network"],
            "live_cash_call_count": transport_counts["live_cash"],
            "indeterminate_resources_retained": True,
            "late_fill_recorded_and_quarantined": True,
            "append_only_settlement_verified": True,
            "same_cid_only": True,
            "blind_repost_count": transport_counts["blind_repost"],
            "completed_at": _iso(completed),
        }
    )


def _kill_switch_observations(
    authority: KillSwitchReadAuthority,
    *,
    context: readiness.ReadinessValidationContext,
    now: datetime,
) -> Mapping[str, Any]:
    native = authority.observe()
    if type(native) is not KillSwitchNativeObservation:
        raise CapturedPaperPreactivationProbeError(
            "KILL_SWITCH_AUTHORITY_INVALID", "kill-switch authority returned shaped evidence"
        )
    version = _nonnegative_int(native.row_id, "kill-switch row id")
    observed = _fresh(native.observed_at, now, seconds=30, field="kill-switch observed_at")
    if native.regime != "kill_switch" or type(native.active) is not bool or version <= 0:
        raise CapturedPaperPreactivationProbeError(
            "KILL_SWITCH_ROW_INVALID", "authoritative kill-switch row is malformed"
        )
    return MappingProxyType(
        {
            "database_target_fingerprint": context.database_target_fingerprint,
            "state_readable": True,
            "active": native.active,
            "state_version": version,
            "observed_at": _iso(observed),
        }
    )


def _rollback_observations(
    authority: RollbackPreactivationBaselineAuthority,
    *,
    context: readiness.ReadinessValidationContext,
    now: datetime,
) -> Mapping[str, Any]:
    native = authority.observe()
    if type(native) is not RollbackNativeObservation:
        raise CapturedPaperPreactivationProbeError(
            "ROLLBACK_AUTHORITY_INVALID", "rollback authority returned shaped hashes"
        )
    task = _strict_json(native.task_snapshot, "task snapshot")
    process = _strict_json(native.process_snapshot, "process snapshot")
    restore = _strict_json(native.restore_plan, "restore plan")
    action = _strict_json(native.candidate_action, "candidate action")
    baseline = native.preactivation_baseline
    if type(baseline) is not host_cutover.PreActivationRollbackBaseline:
        raise CapturedPaperPreactivationProbeError(
            "ROLLBACK_AUTHORITY_INVALID",
            "rollback authority did not return a typed preactivation baseline",
        )
    expected_baseline_sha = host_cutover.sha256_json(
        host_cutover.build_preactivation_rollback_baseline_document(baseline)
    )
    if expected_baseline_sha != baseline.baseline_sha256:
        raise CapturedPaperPreactivationProbeError(
            "ROLLBACK_BASELINE_HASH_MISMATCH",
            "preactivation rollback baseline digest mismatched",
        )
    tasks = task.get("tasks")
    if not isinstance(tasks, Mapping) or set(tasks) != set(readiness.REQUIRED_TASKS):
        raise CapturedPaperPreactivationProbeError(
            "ROLLBACK_TASK_ROSTER_MISMATCH", "legacy task snapshot is not exact"
        )
    task_hashes: dict[str, str] = {}
    for name in readiness.REQUIRED_TASKS:
        row = tasks.get(name)
        if not isinstance(row, Mapping):
            raise CapturedPaperPreactivationProbeError(
                "ROLLBACK_TASK_INVALID", f"legacy task {name} is unavailable"
            )
        xml_b64 = row.get("xml_base64")
        if not isinstance(xml_b64, str):
            raise CapturedPaperPreactivationProbeError(
                "ROLLBACK_TASK_INVALID", f"legacy task {name} XML is unavailable"
            )
        try:
            import base64

            xml = base64.b64decode(xml_b64, validate=True)
        except Exception as exc:
            raise CapturedPaperPreactivationProbeError(
                "ROLLBACK_TASK_INVALID", f"legacy task {name} XML is malformed"
            ) from exc
        task_hashes[name] = _sha256_bytes(xml)
        if row.get("xml_sha256") != task_hashes[name]:
            raise CapturedPaperPreactivationProbeError(
                "ROLLBACK_TASK_INVALID", f"legacy task {name} digest mismatched"
            )
    candidate_task_sha = _sha256_bytes(native.candidate_task_xml)
    expected_action = {
        "schema_version": "chili.captured-paper-host-cutover-action.v1",
        "host_cutover_source_sha256": context.source_hashes.get(
            "captured_paper_host_cutover"
        ),
        "launcher_argument_contract_sha256": (
            context.launcher_argument_contract_sha256
        ),
        "candidate_task_xml_sha256": candidate_task_sha,
        "singleton_policy": "one_unified_candidate_host",
    }
    if dict(action) != expected_action:
        raise CapturedPaperPreactivationProbeError(
            "ROLLBACK_ACTION_MISMATCH", "candidate action is not exact"
        )
    baseline_context = baseline.context
    if not (
        isinstance(process.get("processes"), list)
        and isinstance(restore.get("legacy_process_bindings"), list)
        and baseline_context.activation_generation == context.activation_generation
        and baseline_context.expected_account_id == context.expected_account_id
        and baseline_context.host_cutover_source_sha256
        == context.source_hashes.get("captured_paper_host_cutover")
        and baseline_context.launcher_argument_contract_sha256
        == context.launcher_argument_contract_sha256
        and baseline.task_snapshot.artifact_sha256
        == _sha256_bytes(native.task_snapshot)
        and baseline.process_snapshot.artifact_sha256
        == _sha256_bytes(native.process_snapshot)
        and baseline.restore_plan.artifact_sha256
        == _sha256_bytes(native.restore_plan)
        and baseline.candidate_template_sha256 == candidate_task_sha
        and baseline.candidate_action_sha256 == _sha256_bytes(native.candidate_action)
    ):
        raise CapturedPaperPreactivationProbeError(
            "ROLLBACK_PREACTIVATION_BASELINE_INVALID",
            "rollback bytes differ from their typed preactivation baseline",
        )
    captured = _fresh(
        baseline.validated_at,
        now,
        seconds=3600,
        field="rollback baseline validated_at",
    )
    return MappingProxyType(
        {
            "task_snapshot_sha256": _sha256_bytes(native.task_snapshot),
            "scheduled_task_xml_sha256s": task_hashes,
            "legacy_process_snapshot_sha256": _sha256_bytes(native.process_snapshot),
            "restore_plan_sha256": _sha256_bytes(native.restore_plan),
            "host_cutover_source_sha256": _sha(
                context.source_hashes.get("captured_paper_host_cutover"),
                "host cutover source",
            ),
            "launcher_argument_contract_sha256": (
                context.launcher_argument_contract_sha256
            ),
            "candidate_task_xml_sha256": candidate_task_sha,
            "candidate_action_sha256": readiness.sha256_json(expected_action),
            "preactivation_baseline_sha256": baseline.baseline_sha256,
            "validation_mode": host_cutover.PREACTIVATION_ROLLBACK_BASELINE_MODE,
            "singleton_policy": "one_unified_candidate_host",
            "host_mutation_count": 0,
            "final_validate_only_performed": False,
            "captured_at": _iso(captured),
        }
    )


_PRODUCERS: Mapping[str, Callable[..., Mapping[str, Any]]] = MappingProxyType(
    {
        "runtime_settings": _runtime_observations,
        "broker_account": _broker_observations,
        "database_schema": _database_observations,
        "capture_host_smoke": _capture_observations,
        "focused_regressions": _focused_regression_observations,
        "lifecycle_preflight": _lifecycle_observations,
        "kill_switch": _kill_switch_observations,
        "rollback_snapshot": _rollback_observations,
    }
)


def _reject_reparse_chain(path: Path) -> None:
    cursor = path
    while True:
        info = os.lstat(cursor)
        attributes = int(getattr(info, "st_file_attributes", 0) or 0)
        if stat.S_ISLNK(info.st_mode) or attributes & int(
            getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise CapturedPaperPreactivationProbeError(
                "OUTPUT_REPARSE_REJECTED", "probe output traverses a reparse point"
            )
        parent = cursor.parent
        if parent == cursor:
            return
        cursor = parent


def _content_addressed_new(
    root: Path,
    *,
    namespace: Sequence[str],
    document: Mapping[str, Any],
) -> tuple[Path, str, int]:
    raw = _canonical_json_bytes(dict(document))
    digest = _sha256_bytes(raw)
    parent = root.joinpath(*namespace, digest[:2])
    parent.mkdir(parents=True, exist_ok=True)
    _reject_reparse_chain(parent)
    final = parent / f"{digest}.json"
    temporary = parent / f".{digest}.{uuid.uuid4()}.pending"
    try:
        with temporary.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        # Hard-link publication is atomic and create-new on both Windows and
        # POSIX.  Reuse/overwrite is intentionally rejected, even for equal
        # bytes, so one probe run cannot masquerade as another.
        os.link(temporary, final)
    except FileExistsError as exc:
        raise CapturedPaperPreactivationProbeError(
            "OUTPUT_ALREADY_EXISTS", "probe artifact target already exists"
        ) from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return final.resolve(), digest, len(raw)


def _artifact_refs_for_kind(
    *,
    kind: str,
    context: readiness.ReadinessValidationContext,
    observations: Mapping[str, Any],
    observed_at: datetime,
    output_root: Path,
) -> Mapping[str, Mapping[str, Any]]:
    owners = readiness._PROBE_FIELD_OWNERS[kind]
    refs: dict[str, Mapping[str, Any]] = {}
    for source_name in sorted(owners):
        artifact = readiness.build_readiness_probe_artifact(
            kind=kind,
            source_name=source_name,
            context=context,
            observations={name: observations[name] for name in owners[source_name]},
            observed_at=observed_at,
        )
        path, digest, size = _content_addressed_new(
            output_root,
            namespace=("artifacts", kind, source_name),
            document=artifact,
        )
        refs[source_name] = MappingProxyType(
            {"path": str(path), "sha256": digest, "size_bytes": size}
        )
    return MappingProxyType(refs)


def run_trusted_preactivation_probes(
    *,
    context: readiness.ReadinessValidationContext,
    authorities: TrustedProbeAuthorities,
    output_root: str | Path,
    max_age_seconds_by_kind: Mapping[str, int],
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Mapping[str, Any]:
    """Execute all eight fixed probes and publish artifacts/receipts/manifest.

    Publication is all-or-nothing in authority: an exception means no run
    manifest is emitted.  Individual content-addressed artifacts may remain as
    forensic evidence but cannot be consumed without the final manifest.
    """

    if type(context) is not readiness.ReadinessValidationContext:
        raise CapturedPaperPreactivationProbeError(
            "CONTEXT_INVALID", "readiness context is not typed"
        )
    if type(authorities) is not TrustedProbeAuthorities:
        raise CapturedPaperPreactivationProbeError(
            "AUTHORITY_ROSTER_INVALID", "probe authorities are not the exact roster"
        )
    if set(max_age_seconds_by_kind) != set(readiness.PREACTIVATION_KINDS):
        raise CapturedPaperPreactivationProbeError(
            "TTL_ROSTER_INVALID", "readiness TTL roster is not exact"
        )
    root = Path(output_root)
    if not root.is_absolute() or str(root).startswith(("\\\\", "//")):
        raise CapturedPaperPreactivationProbeError(
            "OUTPUT_ROOT_INVALID", "probe output root must be absolute and local"
        )
    root = root.resolve(strict=True)
    _reject_reparse_chain(root)
    if not root.is_dir():
        raise CapturedPaperPreactivationProbeError(
            "OUTPUT_ROOT_INVALID", "probe output root is not a directory"
        )
    observed_at = _utc(wall_clock(), "wall_clock")
    authority_by_kind = {
        kind: getattr(authorities, kind) for kind in readiness.PREACTIVATION_KINDS
    }
    artifact_bindings: dict[str, Mapping[str, Any]] = {}
    receipt_refs: dict[str, Mapping[str, Any]] = {}
    receipt_digests: dict[str, str] = {}
    for kind in sorted(readiness.PREACTIVATION_KINDS):
        ttl = max_age_seconds_by_kind[kind]
        if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl <= 0:
            raise CapturedPaperPreactivationProbeError(
                "TTL_INVALID", f"{kind} TTL is invalid"
            )
        observations = _PRODUCERS[kind](
            authority_by_kind[kind], context=context, now=observed_at
        )
        refs = _artifact_refs_for_kind(
            kind=kind,
            context=context,
            observations=observations,
            observed_at=observed_at,
            output_root=root,
        )
        artifact_bindings[kind] = refs
        receipt = readiness.issue_readiness_receipt_v3_from_artifacts(
            kind=kind,
            context=context,
            artifact_bindings=refs,
            captured_at=observed_at,
            expires_at=observed_at + timedelta(seconds=ttl),
            now=observed_at,
            max_age_seconds=ttl,
        )
        path, digest, size = _content_addressed_new(
            root,
            namespace=("receipts", kind),
            document=receipt,
        )
        receipt_refs[kind] = MappingProxyType(
            {"path": str(path), "sha256": digest, "size_bytes": size}
        )
        receipt_digests[kind] = digest
    manifest: dict[str, Any] = {
        "schema_version": PROBE_RUN_MANIFEST_SCHEMA_VERSION,
        "generated_at": _iso(observed_at),
        "activation_generation": context.activation_generation,
        "account_scope": "alpaca:paper",
        "expected_account_id": context.expected_account_id,
        "code_build_sha256": context.code_build_sha256,
        "effective_config_sha256": context.effective_config_sha256,
        "capture_receipt_sha256": context.capture_receipt_sha256,
        "probe_runner_source_sha256": context.source_hashes[
            "captured_paper_preactivation_probes"
        ],
        "artifact_bindings": {
            kind: {name: dict(ref) for name, ref in refs.items()}
            for kind, refs in sorted(artifact_bindings.items())
        },
        "readiness_receipts": {
            kind: dict(ref) for kind, ref in sorted(receipt_refs.items())
        },
        "readiness_receipt_sha256s": dict(sorted(receipt_digests.items())),
        "live_cash_authorized": False,
        "orders_submitted": False,
    }
    manifest["manifest_sha256"] = readiness.sha256_json(manifest)
    path, digest, size = _content_addressed_new(
        root,
        namespace=("manifests",),
        document=manifest,
    )
    return MappingProxyType(
        {
            "manifest_path": str(path),
            "manifest_sha256": digest,
            "manifest_size_bytes": size,
            "manifest": MappingProxyType(manifest),
        }
    )


def _assert_operational_ttl_contract() -> None:
    """Keep the executable producer TTLs identical to envelope validation."""

    from scripts import captured_paper_activation_contract as activation_contract

    contract_ttls = {
        kind: activation_contract._RECEIPT_MAX_AGE_SECONDS[kind]
        for kind in readiness.PREACTIVATION_KINDS
    }
    if dict(OPERATIONAL_MAX_AGE_SECONDS_BY_KIND) != contract_ttls:
        raise CapturedPaperPreactivationProbeError(
            "PROBE_TTL_CONTRACT_DRIFT",
            "operational probe TTLs differ from activation validation",
        )


def run_operational_preactivation_probe_command(
    *,
    composition_provider: Callable[[], TrustedOperationalProbeComposition],
    output_root: str | Path,
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Mapping[str, Any]:
    """Run the one all-eight probe command from trusted in-process objects.

    ``composition_provider`` is a Python dependency, never a CLI-selected
    import path or serialized payload.  The owning PAPER process supplies its
    live objects directly.  Standalone execution has no such objects and must
    fail closed instead of manufacturing operational evidence.
    """

    if not callable(composition_provider):
        raise CapturedPaperPreactivationProbeError(
            "OPERATIONAL_COMPOSITION_UNAVAILABLE",
            "trusted operational composition is unavailable",
        )
    composition = composition_provider()
    if type(composition) is not TrustedOperationalProbeComposition:
        raise CapturedPaperPreactivationProbeError(
            "OPERATIONAL_COMPOSITION_INVALID",
            "operational composition is not the exact typed authority roster",
        )
    _assert_operational_ttl_contract()
    return run_trusted_preactivation_probes(
        context=composition.context,
        authorities=composition.authorities,
        output_root=output_root,
        max_age_seconds_by_kind=OPERATIONAL_MAX_AGE_SECONDS_BY_KIND,
        wall_clock=wall_clock,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-root",
        required=True,
        help="Existing absolute local directory for content-addressed probe evidence.",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    composition_provider: Callable[[], TrustedOperationalProbeComposition]
    | None = None,
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    args = _parser().parse_args(argv)
    try:
        if composition_provider is None:
            raise CapturedPaperPreactivationProbeError(
                "OPERATIONAL_COMPOSITION_UNAVAILABLE",
                "standalone execution does not own live trusted authorities",
            )
        result = run_operational_preactivation_probe_command(
            composition_provider=composition_provider,
            output_root=args.output_root,
            wall_clock=wall_clock,
        )
    except CapturedPaperPreactivationProbeError as exc:
        print(
            _canonical_json_bytes(
                {
                    "schema_version": (
                        "chili.captured-paper-preactivation-probe-command-error.v1"
                    ),
                    "error_code": exc.code,
                }
            ).decode("utf-8"),
            file=sys.stderr,
        )
        return 2
    print(
        _canonical_json_bytes(
            {
                "schema_version": (
                    "chili.captured-paper-preactivation-probe-command-result.v1"
                ),
                "manifest_path": result["manifest_path"],
                "manifest_sha256": result["manifest_sha256"],
                "manifest_size_bytes": result["manifest_size_bytes"],
            }
        ).decode("utf-8")
    )
    return 0


__all__ = [
    "AlpacaPaperBrokerReadAuthority",
    "BrokerReadAuthority",
    "BoundCaptureHostReadAuthority",
    "CaptureOnlySmokeReadAuthority",
    "CaptureHostNativeObservation",
    "CaptureHostReadAuthority",
    "CapturedPaperPreactivationProbeError",
    "CommandExecution",
    "DatabaseNativeObservation",
    "DatabaseReadAuthority",
    "FOCUSED_COMPILE_RELATIVE_PATHS",
    "FOCUSED_PYTEST_NODE_IDS",
    "FocusedRegressionAuthority",
    "FocusedRegressionNativeObservation",
    "HostCutoverPreactivationBaselineAuthority",
    "InstalledRuntimeSettingsAuthority",
    "KillSwitchNativeObservation",
    "KillSwitchReadAuthority",
    "LIFECYCLE_SCENARIOS",
    "LifecycleNativeObservation",
    "LifecycleScenarioAuthority",
    "OPERATIONAL_MAX_AGE_SECONDS_BY_KIND",
    "PROBE_RUN_MANIFEST_SCHEMA_VERSION",
    "RollbackNativeObservation",
    "RollbackPreactivationBaselineAuthority",
    "SqlAlchemyDatabaseReadAuthority",
    "SqlAlchemyKillSwitchReadAuthority",
    "SubprocessFocusedRegressionAuthority",
    "SubprocessLifecycleScenarioAuthority",
    "TrustedOperationalProbeComposition",
    "RuntimeSettingsAuthority",
    "RuntimeSettingsNativeObservation",
    "TrustedProbeAuthorities",
    "main",
    "run_operational_preactivation_probe_command",
    "run_trusted_preactivation_probes",
]


if __name__ == "__main__":
    raise SystemExit(main())
