"""Typed, content-bound readiness evidence for fake-money Alpaca PAPER.

The activation boundary used to accept a dictionary of caller-supplied
``checks: true`` values.  This module replaces that self-attestation with
strict per-kind evidence.  Every receipt binds its semantic facts, the exact
source-receipt digests used to derive them, and the current code-build source
that issued the receipt.

This module performs no database, broker, provider, process, or task I/O.
Operational probes run elsewhere and pass their already-produced receipt
hashes and normalized facts to :func:`issue_readiness_receipt_v2`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Any, Mapping, Sequence
import uuid


UTC = timezone.utc
READINESS_SCHEMA_PREFIX = "chili.captured-paper-readiness."
READINESS_EVIDENCE_SCHEMA_PREFIX = "chili.captured-paper-readiness-evidence."
READINESS_PROBE_ARTIFACT_SCHEMA_PREFIX = (
    "chili.captured-paper-readiness-probe-artifact."
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REPARSE_ATTRIBUTE = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
_MAX_PROBE_ARTIFACT_BYTES = 4 * 1024 * 1024

PREACTIVATION_KINDS = frozenset(
    {
        "runtime_settings",
        "broker_account",
        "database_schema",
        "capture_host_smoke",
        "focused_regressions",
        "lifecycle_preflight",
        "kill_switch",
        "rollback_snapshot",
    }
)

EXPECTED_ISSUER_ROLES: Mapping[str, str] = MappingProxyType(
    {
        "runtime_settings": "runtime_environment",
        "broker_account": "captured_alpaca_paper_adapter",
        "database_schema": "app_migrations",
        "capture_host_smoke": "iqfeed_capture_bootstrap_preflight",
        "focused_regressions": "activation_contract",
        "lifecycle_preflight": "captured_paper_transport",
        "kill_switch": "activation_service",
        "rollback_snapshot": "captured_paper_host_cutover",
    }
)

EXPECTED_SOURCE_RECEIPTS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "runtime_settings": frozenset(
            {"runtime_environment", "settings_projection", "adaptive_policy"}
        ),
        "broker_account": frozenset(
            {"paper_connection", "account_read", "position_census", "order_census"}
        ),
        "database_schema": frozenset(
            {"schema_probe", "idempotent_rehearsal"}
        ),
        "capture_host_smoke": frozenset(
            {"bootstrap_preflight", "capture_writer_health", "provider_health"}
        ),
        "focused_regressions": frozenset(
            {"compile_report", "targeted_test_report", "side_effect_census"}
        ),
        "lifecycle_preflight": frozenset(
            {
                "ownership_idempotency",
                "indeterminate_submit_retain",
                "late_fill_quarantine",
                "append_only_fill_settlement",
                "same_cid_reconciliation",
                "no_blind_repost",
            }
        ),
        "kill_switch": frozenset({"kill_switch_query"}),
        "rollback_snapshot": frozenset(
            {"task_snapshot", "process_snapshot", "restore_plan", "candidate_action"}
        ),
    }
)

REQUIRED_TASKS = (
    "CHILI-IQFeed-Depth-Bridge-Daily",
    "CHILI-IQFeed-Depth-Bridge-Logon",
    "CHILI-IQFeed-Trade-Bridge-Daily",
    "CHILI-IQFeed-Trade-Bridge-Logon",
)

REQUIRED_DATABASE_TABLES = (
    "alpaca_paper_fill_activities",
    "alpaca_paper_fill_query_observations",
    "alpaca_paper_post_settlement_fill_contradictions",
    "captured_paper_completed_fill_watch",
    "captured_paper_completed_fill_watch_events",
    "captured_paper_post_commit_outbox",
    "captured_paper_post_commit_outbox_events",
)

_COMMON_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "receipt_kind",
        "verdict",
        "captured_at",
        "expires_at",
        "activation_generation",
        "account_scope",
        "expected_account_id",
        "code_build_sha256",
        "effective_config_sha256",
        "capture_receipt_sha256",
        "issuer_source_role",
        "issuer_source_sha256",
        "evidence",
        "evidence_sha256",
        "live_cash_authorized",
        "orders_submitted",
        "receipt_sha256",
    }
)

_COMMON_PROBED_RECEIPT_KEYS = _COMMON_RECEIPT_KEYS | {"artifact_bindings"}
_PROBE_ARTIFACT_KEYS = frozenset(
    {
        "schema_version",
        "readiness_kind",
        "source_name",
        "activation_generation",
        "account_scope",
        "expected_account_id",
        "issuer_source_role",
        "issuer_source_sha256",
        "probe_runner_source_sha256",
        "observed_at",
        "observations",
        "observations_sha256",
        "content_sha256",
    }
)

_EVIDENCE_KEYS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "runtime_settings": frozenset(
            {
                "schema_version",
                "source_receipts",
                "runtime_environment_sha256",
                "settings_projection_sha256",
                "execution_broker",
                "broker_environment",
                "execution_rail",
                "paper_credentials_present",
                "live_cash_credentials_present",
                "cash_broker_environment_keys_present",
                "equity_only",
                "short_authorized",
                "crypto_authorized",
                "first_dip_policy_mode",
                "adaptive_policy_sha256",
                "policy_surfaces",
                "activation_only_dollar_caps",
                "activation_only_symbol_caps",
            }
        ),
        "broker_account": frozenset(
            {
                "schema_version",
                "source_receipts",
                "account_identity_sha256",
                "connection_generation",
                "connection_receipt_sha256",
                "account_status",
                "account_blocked",
                "trading_blocked",
                "transfers_blocked",
                "trade_suspended_by_user",
                "position_count",
                "open_order_count",
                "position_inventory_sha256",
                "open_order_inventory_sha256",
                "observed_at",
                "paper_execution_only",
            }
        ),
        "database_schema": frozenset(
            {
                "schema_version",
                "source_receipts",
                "database_target_fingerprint",
                "migration_roster_sha256",
                "applied_migrations_sha256",
                "latest_migration",
                "migration_count",
                "required_tables",
                "idempotent_rehearsal_pass_count",
                "idempotent_rehearsal_failure_count",
                "observed_at",
            }
        ),
        "capture_host_smoke": frozenset(
            {
                "schema_version",
                "source_receipts",
                "iqfeed_bootstrap_manifest_sha256",
                "capture_store_root",
                "source_hashes",
                "l1_bound",
                "l2_policy",
                "capture_store_writable",
                "dropped_event_count",
                "overflow_count",
                "unreported_gap_count",
                "provider_health_observed_at",
            }
        ),
        "focused_regressions": frozenset(
            {
                "schema_version",
                "source_receipts",
                "code_build_sha256",
                "compile_file_count",
                "compile_failure_count",
                "selected_test_count",
                "passed_test_count",
                "failed_test_count",
                "error_test_count",
                "real_network_call_count",
                "live_cash_call_count",
                "real_broker_post_call_count",
                "completed_at",
            }
        ),
        "lifecycle_preflight": frozenset(
            {
                "schema_version",
                "source_receipts",
                "runtime_scenario_count",
                "passed_scenario_count",
                "failed_scenario_count",
                "fake_transport_call_count",
                "real_network_call_count",
                "live_cash_call_count",
                "indeterminate_resources_retained",
                "late_fill_recorded_and_quarantined",
                "append_only_settlement_verified",
                "same_cid_only",
                "blind_repost_count",
                "completed_at",
            }
        ),
        "kill_switch": frozenset(
            {
                "schema_version",
                "source_receipts",
                "database_target_fingerprint",
                "state_readable",
                "active",
                "state_version",
                "observed_at",
            }
        ),
        "rollback_snapshot": frozenset(
            {
                "schema_version",
                "source_receipts",
                "task_snapshot_sha256",
                "scheduled_task_xml_sha256s",
                "legacy_process_snapshot_sha256",
                "restore_plan_sha256",
                "host_cutover_source_sha256",
                "launcher_argument_contract_sha256",
                "candidate_task_xml_sha256",
                "candidate_action_sha256",
                "preactivation_baseline_sha256",
                "validation_mode",
                "singleton_policy",
                "host_mutation_count",
                "final_validate_only_performed",
                "captured_at",
            }
        ),
    }
)

# Every semantic field in a probed receipt has one raw-artifact owner.  The
# verifier reconstructs the evidence object from these fields; it never trusts
# a caller-supplied PASS boolean or digest in the receipt itself.
_PROBE_FIELD_OWNERS: Mapping[str, Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "runtime_settings": MappingProxyType(
            {
                "runtime_environment": frozenset(
                    {
                        "runtime_environment_sha256",
                        "execution_broker",
                        "broker_environment",
                        "execution_rail",
                        "paper_credentials_present",
                        "live_cash_credentials_present",
                        "cash_broker_environment_keys_present",
                    }
                ),
                "settings_projection": frozenset(
                    {
                        "settings_projection_sha256",
                        "equity_only",
                        "short_authorized",
                        "crypto_authorized",
                        "first_dip_policy_mode",
                        "policy_surfaces",
                        "activation_only_dollar_caps",
                        "activation_only_symbol_caps",
                    }
                ),
                "adaptive_policy": frozenset({"adaptive_policy_sha256"}),
            }
        ),
        "broker_account": MappingProxyType(
            {
                "paper_connection": frozenset(
                    {
                        "account_identity_sha256",
                        "connection_generation",
                        "connection_receipt_sha256",
                        "paper_execution_only",
                    }
                ),
                "account_read": frozenset(
                    {
                        "account_status",
                        "account_blocked",
                        "trading_blocked",
                        "transfers_blocked",
                        "trade_suspended_by_user",
                        "observed_at",
                    }
                ),
                "position_census": frozenset(
                    {"position_count", "position_inventory_sha256"}
                ),
                "order_census": frozenset(
                    {"open_order_count", "open_order_inventory_sha256"}
                ),
            }
        ),
        "database_schema": MappingProxyType(
            {
                "schema_probe": frozenset(
                    {
                        "database_target_fingerprint",
                        "migration_roster_sha256",
                        "applied_migrations_sha256",
                        "latest_migration",
                        "migration_count",
                        "required_tables",
                        "observed_at",
                    }
                ),
                "idempotent_rehearsal": frozenset(
                    {
                        "idempotent_rehearsal_pass_count",
                        "idempotent_rehearsal_failure_count",
                    }
                ),
            }
        ),
        "capture_host_smoke": MappingProxyType(
            {
                "bootstrap_preflight": frozenset(
                    {
                        "iqfeed_bootstrap_manifest_sha256",
                        "capture_store_root",
                        "source_hashes",
                        "l1_bound",
                        "l2_policy",
                    }
                ),
                "capture_writer_health": frozenset(
                    {
                        "capture_store_writable",
                        "dropped_event_count",
                        "overflow_count",
                        "unreported_gap_count",
                    }
                ),
                "provider_health": frozenset({"provider_health_observed_at"}),
            }
        ),
        "focused_regressions": MappingProxyType(
            {
                "compile_report": frozenset(
                    {"code_build_sha256", "compile_file_count", "compile_failure_count"}
                ),
                "targeted_test_report": frozenset(
                    {
                        "selected_test_count",
                        "passed_test_count",
                        "failed_test_count",
                        "error_test_count",
                        "completed_at",
                    }
                ),
                "side_effect_census": frozenset(
                    {
                        "real_network_call_count",
                        "live_cash_call_count",
                        "real_broker_post_call_count",
                    }
                ),
            }
        ),
        "lifecycle_preflight": MappingProxyType(
            {
                "ownership_idempotency": frozenset(
                    {
                        "runtime_scenario_count",
                        "passed_scenario_count",
                        "failed_scenario_count",
                        "fake_transport_call_count",
                        "real_network_call_count",
                        "live_cash_call_count",
                        "completed_at",
                    }
                ),
                "indeterminate_submit_retain": frozenset(
                    {"indeterminate_resources_retained"}
                ),
                "late_fill_quarantine": frozenset(
                    {"late_fill_recorded_and_quarantined"}
                ),
                "append_only_fill_settlement": frozenset(
                    {"append_only_settlement_verified"}
                ),
                "same_cid_reconciliation": frozenset({"same_cid_only"}),
                "no_blind_repost": frozenset({"blind_repost_count"}),
            }
        ),
        "kill_switch": MappingProxyType(
            {
                "kill_switch_query": frozenset(
                    {
                        "database_target_fingerprint",
                        "state_readable",
                        "active",
                        "state_version",
                        "observed_at",
                    }
                )
            }
        ),
        "rollback_snapshot": MappingProxyType(
            {
                "task_snapshot": frozenset(
                    {"task_snapshot_sha256", "scheduled_task_xml_sha256s"}
                ),
                "process_snapshot": frozenset({"legacy_process_snapshot_sha256"}),
                "restore_plan": frozenset({"restore_plan_sha256"}),
                "candidate_action": frozenset(
                    {
                        "host_cutover_source_sha256",
                        "launcher_argument_contract_sha256",
                        "candidate_task_xml_sha256",
                        "candidate_action_sha256",
                        "preactivation_baseline_sha256",
                        "validation_mode",
                        "singleton_policy",
                        "host_mutation_count",
                        "final_validate_only_performed",
                        "captured_at",
                    }
                ),
            }
        ),
    }
)


class CapturedPaperReadinessEvidenceError(RuntimeError):
    """Stable rejection of a typed readiness artifact."""


@dataclass(frozen=True, slots=True)
class ReadinessValidationContext:
    activation_generation: str
    expected_account_id: str
    code_build_sha256: str
    effective_config_sha256: str
    capture_receipt_sha256: str
    runtime_environment_sha256: str
    database_target_fingerprint: str
    iqfeed_bootstrap_manifest_sha256: str
    launcher_argument_contract_sha256: str
    capture_store_root: str
    source_hashes: Mapping[str, str]
    allowed_read_roots: tuple[str, ...] = ()


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CapturedPaperReadinessEvidenceError(
            "readiness evidence is not canonical JSON"
        ) from exc


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _sha(value: Any, field: str) -> str:
    raw = str(value or "").strip().lower()
    if _SHA256_RE.fullmatch(raw) is None:
        raise CapturedPaperReadinessEvidenceError(f"{field} is not SHA-256")
    return raw


def _uuid(value: Any, field: str) -> str:
    raw = str(value or "").strip().lower()
    try:
        parsed = uuid.UUID(raw)
    except (AttributeError, TypeError, ValueError) as exc:
        raise CapturedPaperReadinessEvidenceError(
            f"{field} is not a canonical UUID"
        ) from exc
    if str(parsed) != raw:
        raise CapturedPaperReadinessEvidenceError(
            f"{field} is not a canonical UUID"
        )
    return raw


def _utc(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise CapturedPaperReadinessEvidenceError(
                f"{field} is not an ISO timestamp"
            ) from exc
    else:
        raise CapturedPaperReadinessEvidenceError(f"{field} is missing")
    if parsed.tzinfo is None:
        raise CapturedPaperReadinessEvidenceError(f"{field} is timezone-naive")
    return parsed.astimezone(UTC)


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], field: str) -> None:
    actual = set(value)
    if actual != set(expected):
        raise CapturedPaperReadinessEvidenceError(
            f"{field} keys differ; missing={sorted(set(expected)-actual)} "
            f"extra={sorted(actual-set(expected))}"
        )


def _strict_json(raw: bytes, field: str) -> Mapping[str, Any]:
    def pairs(rows: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in rows:
            if key in result:
                raise CapturedPaperReadinessEvidenceError(
                    f"{field} repeats JSON key {key}"
                )
            result[key] = item
        return result

    def constant(value: str) -> Any:
        raise CapturedPaperReadinessEvidenceError(
            f"{field} contains non-finite JSON {value}"
        )

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CapturedPaperReadinessEvidenceError(
            f"{field} is not strict UTF-8 JSON"
        ) from exc
    if not isinstance(value, Mapping):
        raise CapturedPaperReadinessEvidenceError(f"{field} root is not an object")
    return value


def _reject_reparse_chain(path: Path) -> None:
    cursor = path
    while True:
        try:
            info = os.lstat(cursor)
        except OSError as exc:
            raise CapturedPaperReadinessEvidenceError(
                f"probe path is unavailable: {path}"
            ) from exc
        attributes = int(getattr(info, "st_file_attributes", 0) or 0)
        if stat.S_ISLNK(info.st_mode) or attributes & _REPARSE_ATTRIBUTE:
            raise CapturedPaperReadinessEvidenceError(
                f"probe path traverses a reparse point: {path}"
            )
        parent = cursor.parent
        if parent == cursor:
            return
        cursor = parent


def _reject_network_drive(path: Path) -> None:
    """Reject mapped remote drives; a drive-letter spelling is not local proof."""

    if os.name != "nt":
        return
    import ctypes

    anchor = str(path.anchor or "")
    if anchor and int(ctypes.windll.kernel32.GetDriveTypeW(anchor)) == 4:
        raise CapturedPaperReadinessEvidenceError(
            f"probe path may not use a network drive: {path}"
        )


def _probe_roots(context: ReadinessValidationContext) -> tuple[Path, ...]:
    roots: list[Path] = []
    for index, raw in enumerate(context.allowed_read_roots):
        path = Path(str(raw or ""))
        if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
            raise CapturedPaperReadinessEvidenceError(
                f"allowed probe root {index} is not absolute and local"
            )
        resolved = path.resolve(strict=True)
        _reject_reparse_chain(resolved)
        _reject_network_drive(resolved)
        if not resolved.is_dir():
            raise CapturedPaperReadinessEvidenceError(
                f"allowed probe root {index} is not a directory"
            )
        if resolved not in roots:
            roots.append(resolved)
    if not roots:
        raise CapturedPaperReadinessEvidenceError(
            "probed readiness has no allowed local artifact root"
        )
    return tuple(roots)


def _inside(path: Path, roots: Sequence[Path]) -> bool:
    return any(
        _relative is not None
        for root in roots
        for _relative in (
            (path.relative_to(root) if path == root or root in path.parents else None),
        )
    )


def _stable_probe_read(
    reference: Mapping[str, Any],
    *,
    roots: Sequence[Path],
    field: str,
) -> tuple[Path, bytes, str, int]:
    _exact_keys(reference, frozenset({"path", "sha256", "size_bytes"}), field)
    raw_path = str(reference.get("path") or "")
    path = Path(raw_path)
    if not path.is_absolute() or raw_path.startswith(("\\\\", "//")):
        raise CapturedPaperReadinessEvidenceError(
            f"{field}.path is not absolute and local"
        )
    resolved = path.resolve(strict=True)
    _reject_reparse_chain(resolved)
    if str(resolved) != raw_path or not _inside(resolved, roots):
        raise CapturedPaperReadinessEvidenceError(
            f"{field}.path is not canonical or escaped its local roots"
        )
    before = os.stat(resolved, follow_symlinks=False)
    declared_size = _nonnegative_int(reference.get("size_bytes"), f"{field}.size_bytes")
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_size <= 0
        or before.st_size > _MAX_PROBE_ARTIFACT_BYTES
        or before.st_size != declared_size
    ):
        raise CapturedPaperReadinessEvidenceError(
            f"{field} is not the declared bounded regular file"
        )
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    total = 0
    with resolved.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_PROBE_ARTIFACT_BYTES:
                raise CapturedPaperReadinessEvidenceError(
                    f"{field} grew beyond the bounded read size"
                )
            digest.update(chunk)
            chunks.append(chunk)
    after = os.stat(resolved, follow_symlinks=False)
    if (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or total != after.st_size
    ):
        raise CapturedPaperReadinessEvidenceError(f"{field} changed while read")
    actual_sha = digest.hexdigest()
    if actual_sha != _sha(reference.get("sha256"), f"{field}.sha256"):
        raise CapturedPaperReadinessEvidenceError(f"{field} content hash mismatch")
    return resolved, b"".join(chunks), actual_sha, total


def _reconstruct_probed_evidence(
    *,
    kind: str,
    artifact_bindings: Mapping[str, Any],
    context: ReadinessValidationContext,
    captured_at: datetime,
    max_age_seconds: int,
) -> tuple[Mapping[str, Any], Mapping[str, Mapping[str, Any]]]:
    owners = _PROBE_FIELD_OWNERS.get(kind)
    if owners is None:
        raise CapturedPaperReadinessEvidenceError(
            f"{kind} has no verifier-owned probe contract"
        )
    expected_sources = EXPECTED_SOURCE_RECEIPTS[kind]
    _exact_keys(artifact_bindings, expected_sources, f"{kind}.artifact_bindings")
    roots = _probe_roots(context)
    evidence: dict[str, Any] = {
        "schema_version": f"{READINESS_EVIDENCE_SCHEMA_PREFIX}{kind}.v3",
        "source_receipts": {},
    }
    normalized: dict[str, Mapping[str, Any]] = {}
    seen_paths: set[Path] = set()
    for source_name in sorted(expected_sources):
        reference = artifact_bindings.get(source_name)
        if not isinstance(reference, Mapping):
            raise CapturedPaperReadinessEvidenceError(
                f"{kind}.{source_name} probe reference is missing"
            )
        path, raw, actual_sha, size = _stable_probe_read(
            reference,
            roots=roots,
            field=f"{kind}.artifact_bindings.{source_name}",
        )
        if path in seen_paths:
            raise CapturedPaperReadinessEvidenceError(
                f"{kind} reuses one raw artifact for multiple probes"
            )
        seen_paths.add(path)
        document = _strict_json(raw, f"{kind}.{source_name}.artifact")
        _exact_keys(document, _PROBE_ARTIFACT_KEYS, f"{kind}.{source_name}.artifact")
        expected_exact = {
            "schema_version": (
                f"{READINESS_PROBE_ARTIFACT_SCHEMA_PREFIX}{kind}.{source_name}.v2"
            ),
            "readiness_kind": kind,
            "source_name": source_name,
            "activation_generation": _uuid(
                context.activation_generation, "context.activation_generation"
            ),
            "account_scope": "alpaca:paper",
            "expected_account_id": _uuid(
                context.expected_account_id, "context.expected_account_id"
            ),
            "issuer_source_role": EXPECTED_ISSUER_ROLES[kind],
            "issuer_source_sha256": _sha(
                context.source_hashes.get(EXPECTED_ISSUER_ROLES[kind]),
                "context.issuer_source_sha256",
            ),
            "probe_runner_source_sha256": _sha(
                context.source_hashes.get("captured_paper_preactivation_probes"),
                "context.probe_runner_source_sha256",
            ),
        }
        if any(document.get(name) != value for name, value in expected_exact.items()):
            raise CapturedPaperReadinessEvidenceError(
                f"{kind}.{source_name} raw artifact has foreign provenance"
            )
        observed_at = _utc(document.get("observed_at"), f"{kind}.{source_name}.observed_at")
        artifact_age = (captured_at - observed_at).total_seconds()
        if artifact_age < 0 or artifact_age > max_age_seconds:
            raise CapturedPaperReadinessEvidenceError(
                f"{kind}.{source_name} raw artifact is stale or future-dated"
            )
        observations = document.get("observations")
        if not isinstance(observations, Mapping):
            raise CapturedPaperReadinessEvidenceError(
                f"{kind}.{source_name}.observations is not an object"
            )
        owned_fields = owners[source_name]
        _exact_keys(
            observations,
            owned_fields,
            f"{kind}.{source_name}.observations",
        )
        if sha256_json(observations) != _sha(
            document.get("observations_sha256"),
            f"{kind}.{source_name}.observations_sha256",
        ):
            raise CapturedPaperReadinessEvidenceError(
                f"{kind}.{source_name} observations digest mismatch"
            )
        body = dict(document)
        claimed_content_sha = _sha(
            body.pop("content_sha256", None),
            f"{kind}.{source_name}.content_sha256",
        )
        if sha256_json(body) != claimed_content_sha:
            raise CapturedPaperReadinessEvidenceError(
                f"{kind}.{source_name} raw artifact content digest mismatch"
            )
        overlap = set(evidence) & set(observations)
        if overlap:
            raise CapturedPaperReadinessEvidenceError(
                f"{kind} probe observations overlap: {sorted(overlap)}"
            )
        evidence.update(dict(observations))
        evidence["source_receipts"][source_name] = actual_sha
        normalized[source_name] = MappingProxyType(
            {"path": str(path), "sha256": actual_sha, "size_bytes": size}
        )
    expected_fields = _EVIDENCE_KEYS[kind]
    if set(evidence) != set(expected_fields):
        raise CapturedPaperReadinessEvidenceError(
            f"{kind} probe field ownership is incomplete"
        )
    return MappingProxyType(evidence), MappingProxyType(normalized)


def _nonnegative_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CapturedPaperReadinessEvidenceError(
            f"{field} is not a non-negative integer"
        )
    return value


def _source_receipts(kind: str, evidence: Mapping[str, Any]) -> Mapping[str, str]:
    values = evidence.get("source_receipts")
    if not isinstance(values, Mapping):
        raise CapturedPaperReadinessEvidenceError(
            f"{kind} source receipts are missing"
        )
    expected = EXPECTED_SOURCE_RECEIPTS[kind]
    _exact_keys(values, expected, f"{kind}.source_receipts")
    return MappingProxyType(
        {name: _sha(values.get(name), f"{kind}.source_receipts.{name}") for name in expected}
    )


def _evidence_time(
    evidence: Mapping[str, Any],
    field: str,
    *,
    captured_at: datetime,
    max_age_seconds: int,
) -> datetime:
    observed = _utc(evidence.get(field), f"evidence.{field}")
    age = (captured_at - observed).total_seconds()
    if age < 0 or age > max_age_seconds:
        raise CapturedPaperReadinessEvidenceError(
            f"evidence.{field} is stale or future-dated"
        )
    return observed


def _validate_runtime(
    evidence: Mapping[str, Any], context: ReadinessValidationContext
) -> None:
    sources = _source_receipts("runtime_settings", evidence)
    exact = {
        "runtime_environment_sha256": context.runtime_environment_sha256,
        "settings_projection_sha256": context.effective_config_sha256,
        "execution_broker": "alpaca",
        "broker_environment": "paper",
        "execution_rail": "alpaca",
        "paper_credentials_present": True,
        "live_cash_credentials_present": False,
        "cash_broker_environment_keys_present": False,
        "equity_only": True,
        "short_authorized": False,
        "crypto_authorized": False,
        "first_dip_policy_mode": "candidate",
        "policy_surfaces": ["captured_paper", "replay_v3"],
        "activation_only_dollar_caps": [],
        "activation_only_symbol_caps": [],
    }
    if any(evidence.get(name) != value for name, value in exact.items()):
        raise CapturedPaperReadinessEvidenceError(
            "runtime settings escaped Alpaca PAPER policy parity"
        )
    policy_sha = _sha(evidence.get("adaptive_policy_sha256"), "adaptive_policy_sha256")
    if evidence.get("schema_version", "").endswith(".v2") and (
        sources["runtime_environment"] != context.runtime_environment_sha256
        or sources["settings_projection"] != context.effective_config_sha256
        or sources["adaptive_policy"] != policy_sha
    ):
        raise CapturedPaperReadinessEvidenceError(
            "runtime source receipts do not bind the projected policy"
        )


def _validate_broker(
    evidence: Mapping[str, Any],
    context: ReadinessValidationContext,
    *,
    captured_at: datetime,
) -> None:
    sources = _source_receipts("broker_account", evidence)
    expected_identity = sha256_json(
        {
            "account_id": context.expected_account_id,
            "broker": "alpaca",
            "environment": "paper",
        }
    )
    generation = str(evidence.get("connection_generation") or "")
    empty_inventory = hashlib.sha256(b"[]").hexdigest()
    if not generation.startswith("alpaca-paper-rest:"):
        raise CapturedPaperReadinessEvidenceError(
            "broker connection is not Alpaca PAPER"
        )
    _sha(generation.split(":", 1)[1], "broker connection generation")
    connection_receipt = _sha(
        evidence.get("connection_receipt_sha256"),
        "broker connection receipt",
    )
    exact = {
        "account_identity_sha256": expected_identity,
        "account_status": "ACTIVE",
        "account_blocked": False,
        "trading_blocked": False,
        "transfers_blocked": False,
        "trade_suspended_by_user": False,
        "position_count": 0,
        "open_order_count": 0,
        "position_inventory_sha256": empty_inventory,
        "open_order_inventory_sha256": empty_inventory,
        "paper_execution_only": True,
    }
    if any(evidence.get(name) != value for name, value in exact.items()):
        raise CapturedPaperReadinessEvidenceError(
            "broker evidence is not active, unblocked, flat Alpaca PAPER"
        )
    if (
        evidence.get("schema_version", "").endswith(".v2")
        and sources["paper_connection"] != connection_receipt
    ):
        raise CapturedPaperReadinessEvidenceError(
            "broker connection generation is not source-receipt bound"
        )
    _evidence_time(evidence, "observed_at", captured_at=captured_at, max_age_seconds=30)


def _validate_database(
    evidence: Mapping[str, Any],
    context: ReadinessValidationContext,
    *,
    captured_at: datetime,
) -> None:
    sources = _source_receipts("database_schema", evidence)
    roster = _sha(evidence.get("migration_roster_sha256"), "migration_roster_sha256")
    applied = _sha(
        evidence.get("applied_migrations_sha256"), "applied_migrations_sha256"
    )
    count = _nonnegative_int(evidence.get("migration_count"), "migration_count")
    passes = _nonnegative_int(
        evidence.get("idempotent_rehearsal_pass_count"),
        "idempotent_rehearsal_pass_count",
    )
    failures = _nonnegative_int(
        evidence.get("idempotent_rehearsal_failure_count"),
        "idempotent_rehearsal_failure_count",
    )
    if not (
        evidence.get("database_target_fingerprint")
        == context.database_target_fingerprint
        and roster == applied
        and count > 0
        and isinstance(evidence.get("latest_migration"), str)
        and bool(str(evidence.get("latest_migration") or "").strip())
        and evidence.get("required_tables") == list(REQUIRED_DATABASE_TABLES)
        and passes > 0
        and failures == 0
        and (
            not evidence.get("schema_version", "").endswith(".v2")
            or sources["schema_probe"] == applied
        )
    ):
        raise CapturedPaperReadinessEvidenceError(
            "database evidence is not exact, complete, and rehearsed"
        )
    _evidence_time(evidence, "observed_at", captured_at=captured_at, max_age_seconds=300)


def _validate_capture(
    evidence: Mapping[str, Any],
    context: ReadinessValidationContext,
    *,
    captured_at: datetime,
) -> None:
    sources = _source_receipts("capture_host_smoke", evidence)
    source_hashes = evidence.get("source_hashes")
    required_roles = (
        "iqfeed_capture_host",
        "iqfeed_trade_bridge",
        "iqfeed_depth_bridge",
        "iqfeed_l1_capture",
        "iqfeed_l2_capture",
    )
    if not isinstance(source_hashes, Mapping):
        raise CapturedPaperReadinessEvidenceError("capture source hashes are missing")
    _exact_keys(source_hashes, frozenset(required_roles), "capture.source_hashes")
    if any(
        _sha(source_hashes.get(role), f"capture.source_hashes.{role}")
        != context.source_hashes.get(role)
        for role in required_roles
    ):
        raise CapturedPaperReadinessEvidenceError(
            "capture smoke is not bound to the candidate code build"
        )
    exact = {
        "iqfeed_bootstrap_manifest_sha256": context.iqfeed_bootstrap_manifest_sha256,
        "capture_store_root": context.capture_store_root,
        "l1_bound": True,
        "l2_policy": "decision_local_fail_closed",
        "capture_store_writable": True,
        "dropped_event_count": 0,
        "overflow_count": 0,
        "unreported_gap_count": 0,
    }
    if any(evidence.get(name) != value for name, value in exact.items()):
        raise CapturedPaperReadinessEvidenceError(
            "capture smoke is unhealthy or silently reduced fidelity"
        )
    if (
        evidence.get("schema_version", "").endswith(".v2")
        and sources["bootstrap_preflight"]
        != context.iqfeed_bootstrap_manifest_sha256
    ):
        raise CapturedPaperReadinessEvidenceError(
            "capture preflight is not bound to the bootstrap manifest"
        )
    _evidence_time(
        evidence,
        "provider_health_observed_at",
        captured_at=captured_at,
        max_age_seconds=60,
    )


def _validate_regressions(
    evidence: Mapping[str, Any],
    context: ReadinessValidationContext,
    *,
    captured_at: datetime,
) -> None:
    _source_receipts("focused_regressions", evidence)
    counts = {
        name: _nonnegative_int(evidence.get(name), name)
        for name in (
            "compile_file_count",
            "compile_failure_count",
            "selected_test_count",
            "passed_test_count",
            "failed_test_count",
            "error_test_count",
            "real_network_call_count",
            "live_cash_call_count",
            "real_broker_post_call_count",
        )
    }
    if not (
        evidence.get("code_build_sha256") == context.code_build_sha256
        and counts["compile_file_count"] > 0
        and counts["compile_failure_count"] == 0
        and counts["selected_test_count"] > 0
        and counts["passed_test_count"] == counts["selected_test_count"]
        and counts["failed_test_count"] == 0
        and counts["error_test_count"] == 0
        and counts["real_network_call_count"] == 0
        and counts["live_cash_call_count"] == 0
        and counts["real_broker_post_call_count"] == 0
    ):
        raise CapturedPaperReadinessEvidenceError(
            "focused regression evidence is incomplete or side-effectful"
        )
    _evidence_time(evidence, "completed_at", captured_at=captured_at, max_age_seconds=3600)


def _validate_lifecycle(
    evidence: Mapping[str, Any],
    _context: ReadinessValidationContext,
    *,
    captured_at: datetime,
) -> None:
    _source_receipts("lifecycle_preflight", evidence)
    counts = {
        name: _nonnegative_int(evidence.get(name), name)
        for name in (
            "runtime_scenario_count",
            "passed_scenario_count",
            "failed_scenario_count",
            "fake_transport_call_count",
            "real_network_call_count",
            "live_cash_call_count",
            "blind_repost_count",
        )
    }
    if not (
        counts["runtime_scenario_count"] >= len(
            EXPECTED_SOURCE_RECEIPTS["lifecycle_preflight"]
        )
        and counts["passed_scenario_count"] == counts["runtime_scenario_count"]
        and counts["failed_scenario_count"] == 0
        and counts["fake_transport_call_count"] > 0
        and counts["real_network_call_count"] == 0
        and counts["live_cash_call_count"] == 0
        and evidence.get("indeterminate_resources_retained") is True
        and evidence.get("late_fill_recorded_and_quarantined") is True
        and evidence.get("append_only_settlement_verified") is True
        and evidence.get("same_cid_only") is True
        and counts["blind_repost_count"] == 0
    ):
        raise CapturedPaperReadinessEvidenceError(
            "lifecycle preflight did not exercise the required runtime invariants"
        )
    _evidence_time(evidence, "completed_at", captured_at=captured_at, max_age_seconds=300)


def _validate_kill_switch(
    evidence: Mapping[str, Any],
    context: ReadinessValidationContext,
    *,
    captured_at: datetime,
) -> None:
    _source_receipts("kill_switch", evidence)
    version = _nonnegative_int(evidence.get("state_version"), "state_version")
    if not (
        evidence.get("database_target_fingerprint")
        == context.database_target_fingerprint
        and evidence.get("state_readable") is True
        and evidence.get("active") is False
        and version > 0
    ):
        raise CapturedPaperReadinessEvidenceError(
            "kill switch is unreadable, active, or bound to another database"
        )
    _evidence_time(evidence, "observed_at", captured_at=captured_at, max_age_seconds=30)


def _validate_rollback(
    evidence: Mapping[str, Any],
    context: ReadinessValidationContext,
    *,
    captured_at: datetime,
) -> None:
    sources = _source_receipts("rollback_snapshot", evidence)
    tasks = evidence.get("scheduled_task_xml_sha256s")
    if not isinstance(tasks, Mapping):
        raise CapturedPaperReadinessEvidenceError("rollback task snapshot is missing")
    _exact_keys(tasks, frozenset(REQUIRED_TASKS), "rollback.scheduled_tasks")
    for task in REQUIRED_TASKS:
        _sha(tasks.get(task), f"rollback.task.{task}")
    _sha(evidence.get("task_snapshot_sha256"), "task_snapshot_sha256")
    process_sha = _sha(
        evidence.get("legacy_process_snapshot_sha256"),
        "legacy_process_snapshot_sha256",
    )
    restore_sha = _sha(evidence.get("restore_plan_sha256"), "restore_plan_sha256")
    action_sha = _sha(
        evidence.get("candidate_action_sha256"), "candidate_action_sha256"
    )
    host_cutover_sha = _sha(
        evidence.get("host_cutover_source_sha256"),
        "host_cutover_source_sha256",
    )
    launcher_contract_sha = _sha(
        evidence.get("launcher_argument_contract_sha256"),
        "launcher_argument_contract_sha256",
    )
    candidate_task_xml_sha = _sha(
        evidence.get("candidate_task_xml_sha256"),
        "candidate_task_xml_sha256",
    )
    _sha(
        evidence.get("preactivation_baseline_sha256"),
        "preactivation_baseline_sha256",
    )
    expected_action_sha = sha256_json(
        {
            "schema_version": "chili.captured-paper-host-cutover-action.v1",
            "host_cutover_source_sha256": host_cutover_sha,
            "launcher_argument_contract_sha256": launcher_contract_sha,
            "candidate_task_xml_sha256": candidate_task_xml_sha,
            "singleton_policy": "one_unified_candidate_host",
        }
    )
    mutation_count = _nonnegative_int(
        evidence.get("host_mutation_count"), "host_mutation_count"
    )
    if not (
        (
            not evidence.get("schema_version", "").endswith(".v2")
            or (
                process_sha == sources["process_snapshot"]
                and restore_sha == sources["restore_plan"]
                and action_sha == sources["candidate_action"]
            )
        )
        and host_cutover_sha
        == context.source_hashes.get("captured_paper_host_cutover")
        and launcher_contract_sha == context.launcher_argument_contract_sha256
        and action_sha == expected_action_sha
        and evidence.get("singleton_policy") == "one_unified_candidate_host"
        and evidence.get("validation_mode")
        == "PREACTIVATION_ROLLBACK_BASELINE"
        and mutation_count == 0
        and evidence.get("final_validate_only_performed") is False
    ):
        raise CapturedPaperReadinessEvidenceError(
            "rollback snapshot is not an exact read-only preactivation baseline"
        )
    _evidence_time(evidence, "captured_at", captured_at=captured_at, max_age_seconds=3600)


_VALIDATORS = {
    "runtime_settings": _validate_runtime,
    "broker_account": _validate_broker,
    "database_schema": _validate_database,
    "capture_host_smoke": _validate_capture,
    "focused_regressions": _validate_regressions,
    "lifecycle_preflight": _validate_lifecycle,
    "kill_switch": _validate_kill_switch,
    "rollback_snapshot": _validate_rollback,
}


def validate_readiness_receipt_v2(
    document: Mapping[str, Any],
    *,
    kind: str,
    context: ReadinessValidationContext,
    now: datetime,
    max_age_seconds: int,
) -> tuple[datetime, datetime]:
    """Validate one exact v2 preactivation receipt and semantic evidence."""

    if kind not in PREACTIVATION_KINDS:
        raise CapturedPaperReadinessEvidenceError(
            "readiness kind is not a preactivation kind"
        )
    if not isinstance(document, Mapping):
        raise CapturedPaperReadinessEvidenceError("readiness receipt is not an object")
    _exact_keys(document, _COMMON_RECEIPT_KEYS, f"readiness.{kind}")
    if not isinstance(context, ReadinessValidationContext):
        raise CapturedPaperReadinessEvidenceError("readiness context is malformed")
    expected_schema = f"{READINESS_SCHEMA_PREFIX}{kind}.v2"
    exact = {
        "schema_version": expected_schema,
        "receipt_kind": kind,
        "verdict": "PASS",
        "activation_generation": _uuid(
            context.activation_generation, "context.activation_generation"
        ),
        "account_scope": "alpaca:paper",
        "expected_account_id": _uuid(
            context.expected_account_id, "context.expected_account_id"
        ),
        "code_build_sha256": _sha(
            context.code_build_sha256, "context.code_build_sha256"
        ),
        "effective_config_sha256": _sha(
            context.effective_config_sha256, "context.effective_config_sha256"
        ),
        "capture_receipt_sha256": _sha(
            context.capture_receipt_sha256, "context.capture_receipt_sha256"
        ),
        "issuer_source_role": EXPECTED_ISSUER_ROLES[kind],
        "live_cash_authorized": False,
        "orders_submitted": False,
    }
    if any(document.get(name) != value for name, value in exact.items()):
        raise CapturedPaperReadinessEvidenceError(
            f"{kind} readiness binding or authority is invalid"
        )
    issuer_sha = _sha(document.get("issuer_source_sha256"), "issuer_source_sha256")
    if issuer_sha != context.source_hashes.get(EXPECTED_ISSUER_ROLES[kind]):
        raise CapturedPaperReadinessEvidenceError(
            f"{kind} issuer is not bound to the candidate code build"
        )
    captured_at = _utc(document.get("captured_at"), f"{kind}.captured_at")
    expires_at = _utc(document.get("expires_at"), f"{kind}.expires_at")
    current = _utc(now, "now")
    age = (current - captured_at).total_seconds()
    if (
        age < 0
        or age > max_age_seconds
        or expires_at < current
        or expires_at <= captured_at
        or (expires_at - captured_at).total_seconds() > max_age_seconds
    ):
        raise CapturedPaperReadinessEvidenceError(
            f"{kind} readiness receipt is stale, future-dated, or overlong"
        )
    evidence = document.get("evidence")
    if not isinstance(evidence, Mapping):
        raise CapturedPaperReadinessEvidenceError(f"{kind} evidence is missing")
    _exact_keys(evidence, _EVIDENCE_KEYS[kind], f"{kind}.evidence")
    if evidence.get("schema_version") != (
        f"{READINESS_EVIDENCE_SCHEMA_PREFIX}{kind}.v2"
    ):
        raise CapturedPaperReadinessEvidenceError(
            f"{kind} evidence schema is unsupported"
        )
    evidence_sha = _sha(document.get("evidence_sha256"), "evidence_sha256")
    if sha256_json(evidence) != evidence_sha:
        raise CapturedPaperReadinessEvidenceError(
            f"{kind} evidence digest mismatch"
        )
    validator = _VALIDATORS[kind]
    if kind == "runtime_settings":
        validator(evidence, context)
    else:
        validator(evidence, context, captured_at=captured_at)
    claimed_receipt = _sha(document.get("receipt_sha256"), "receipt_sha256")
    body = dict(document)
    body.pop("receipt_sha256")
    if sha256_json(body) != claimed_receipt:
        raise CapturedPaperReadinessEvidenceError(
            f"{kind} readiness receipt digest mismatch"
        )
    return captured_at, expires_at


def validate_readiness_receipt_v3(
    document: Mapping[str, Any],
    *,
    kind: str,
    context: ReadinessValidationContext,
    now: datetime,
    max_age_seconds: int,
) -> tuple[datetime, datetime]:
    """Validate evidence reconstructed from verifier-owned local artifacts."""

    if kind not in PREACTIVATION_KINDS:
        raise CapturedPaperReadinessEvidenceError(
            "readiness kind is not a preactivation kind"
        )
    if not isinstance(document, Mapping):
        raise CapturedPaperReadinessEvidenceError("readiness receipt is not an object")
    _exact_keys(document, _COMMON_PROBED_RECEIPT_KEYS, f"readiness.{kind}")
    expected = {
        "schema_version": f"{READINESS_SCHEMA_PREFIX}{kind}.v3",
        "receipt_kind": kind,
        "verdict": "PASS",
        "activation_generation": _uuid(
            context.activation_generation, "context.activation_generation"
        ),
        "account_scope": "alpaca:paper",
        "expected_account_id": _uuid(
            context.expected_account_id, "context.expected_account_id"
        ),
        "code_build_sha256": _sha(
            context.code_build_sha256, "context.code_build_sha256"
        ),
        "effective_config_sha256": _sha(
            context.effective_config_sha256, "context.effective_config_sha256"
        ),
        "capture_receipt_sha256": _sha(
            context.capture_receipt_sha256, "context.capture_receipt_sha256"
        ),
        "issuer_source_role": EXPECTED_ISSUER_ROLES[kind],
        "live_cash_authorized": False,
        "orders_submitted": False,
    }
    if any(document.get(name) != value for name, value in expected.items()):
        raise CapturedPaperReadinessEvidenceError(
            f"{kind} probed readiness binding or authority is invalid"
        )
    issuer_sha = _sha(document.get("issuer_source_sha256"), "issuer_source_sha256")
    if issuer_sha != context.source_hashes.get(EXPECTED_ISSUER_ROLES[kind]):
        raise CapturedPaperReadinessEvidenceError(
            f"{kind} probe issuer is not bound to the candidate code build"
        )
    captured_at = _utc(document.get("captured_at"), f"{kind}.captured_at")
    expires_at = _utc(document.get("expires_at"), f"{kind}.expires_at")
    current = _utc(now, "now")
    age = (current - captured_at).total_seconds()
    if (
        age < 0
        or age > max_age_seconds
        or expires_at < current
        or expires_at <= captured_at
        or (expires_at - captured_at).total_seconds() > max_age_seconds
    ):
        raise CapturedPaperReadinessEvidenceError(
            f"{kind} probed readiness receipt is stale, future-dated, or overlong"
        )
    artifact_bindings = document.get("artifact_bindings")
    if not isinstance(artifact_bindings, Mapping):
        raise CapturedPaperReadinessEvidenceError(
            f"{kind} artifact bindings are missing"
        )
    reconstructed, normalized_bindings = _reconstruct_probed_evidence(
        kind=kind,
        artifact_bindings=artifact_bindings,
        context=context,
        captured_at=captured_at,
        max_age_seconds=max_age_seconds,
    )
    if canonical_json_bytes(artifact_bindings) != canonical_json_bytes(
        {name: dict(reference) for name, reference in normalized_bindings.items()}
    ):
        raise CapturedPaperReadinessEvidenceError(
            f"{kind} artifact bindings are not canonical"
        )
    evidence = document.get("evidence")
    if not isinstance(evidence, Mapping) or canonical_json_bytes(
        evidence
    ) != canonical_json_bytes(dict(reconstructed)):
        raise CapturedPaperReadinessEvidenceError(
            f"{kind} evidence was not derived from its raw artifacts"
        )
    evidence_sha = _sha(document.get("evidence_sha256"), "evidence_sha256")
    if sha256_json(dict(reconstructed)) != evidence_sha:
        raise CapturedPaperReadinessEvidenceError(
            f"{kind} reconstructed evidence digest mismatch"
        )
    validator = _VALIDATORS[kind]
    if kind == "runtime_settings":
        validator(reconstructed, context)
    else:
        validator(reconstructed, context, captured_at=captured_at)
    claimed_receipt = _sha(document.get("receipt_sha256"), "receipt_sha256")
    body = dict(document)
    body.pop("receipt_sha256")
    if sha256_json(body) != claimed_receipt:
        raise CapturedPaperReadinessEvidenceError(
            f"{kind} probed readiness receipt digest mismatch"
        )
    return captured_at, expires_at


def issue_readiness_receipt_v3_from_artifacts(
    *,
    kind: str,
    context: ReadinessValidationContext,
    artifact_bindings: Mapping[str, Any],
    captured_at: datetime,
    expires_at: datetime,
    now: datetime | None = None,
    max_age_seconds: int,
) -> Mapping[str, Any]:
    """Issue a preactivation receipt only from parsed, hash-bound raw files."""

    captured = _utc(captured_at, "captured_at")
    expires = _utc(expires_at, "expires_at")
    reconstructed, normalized_bindings = _reconstruct_probed_evidence(
        kind=kind,
        artifact_bindings=artifact_bindings,
        context=context,
        captured_at=captured,
        max_age_seconds=max_age_seconds,
    )
    body: dict[str, Any] = {
        "schema_version": f"{READINESS_SCHEMA_PREFIX}{kind}.v3",
        "receipt_kind": kind,
        "verdict": "PASS",
        "captured_at": captured.isoformat(),
        "expires_at": expires.isoformat(),
        "activation_generation": context.activation_generation,
        "account_scope": "alpaca:paper",
        "expected_account_id": context.expected_account_id,
        "code_build_sha256": context.code_build_sha256,
        "effective_config_sha256": context.effective_config_sha256,
        "capture_receipt_sha256": context.capture_receipt_sha256,
        "issuer_source_role": EXPECTED_ISSUER_ROLES[kind],
        "issuer_source_sha256": context.source_hashes[EXPECTED_ISSUER_ROLES[kind]],
        "artifact_bindings": {
            name: dict(reference) for name, reference in normalized_bindings.items()
        },
        "evidence": dict(reconstructed),
        "evidence_sha256": sha256_json(dict(reconstructed)),
        "live_cash_authorized": False,
        "orders_submitted": False,
    }
    body["receipt_sha256"] = sha256_json(body)
    validate_readiness_receipt_v3(
        body,
        kind=kind,
        context=context,
        now=now or captured,
        max_age_seconds=max_age_seconds,
    )
    return MappingProxyType(body)


def build_readiness_probe_artifact(
    *,
    kind: str,
    source_name: str,
    context: ReadinessValidationContext,
    observations: Mapping[str, Any],
    observed_at: datetime,
) -> Mapping[str, Any]:
    """Build the exact raw envelope a trusted local probe must persist.

    This does not issue readiness.  The persisted bytes still have to pass the
    independent bounded-file loader and the full kind-specific validator.
    """

    owners = _PROBE_FIELD_OWNERS.get(kind)
    if owners is None or source_name not in owners:
        raise CapturedPaperReadinessEvidenceError(
            "probe kind/source is unsupported"
        )
    if not isinstance(observations, Mapping):
        raise CapturedPaperReadinessEvidenceError("probe observations are not an object")
    _exact_keys(
        observations,
        owners[source_name],
        f"{kind}.{source_name}.observations",
    )
    observed = _utc(observed_at, "observed_at")
    body: dict[str, Any] = {
        "schema_version": (
            f"{READINESS_PROBE_ARTIFACT_SCHEMA_PREFIX}{kind}.{source_name}.v2"
        ),
        "readiness_kind": kind,
        "source_name": source_name,
        "activation_generation": context.activation_generation,
        "account_scope": "alpaca:paper",
        "expected_account_id": context.expected_account_id,
        "issuer_source_role": EXPECTED_ISSUER_ROLES[kind],
        "issuer_source_sha256": context.source_hashes[EXPECTED_ISSUER_ROLES[kind]],
        "probe_runner_source_sha256": context.source_hashes[
            "captured_paper_preactivation_probes"
        ],
        "observed_at": observed.isoformat(),
        "observations": json.loads(canonical_json_bytes(observations).decode("utf-8")),
        "observations_sha256": sha256_json(observations),
    }
    body["content_sha256"] = sha256_json(body)
    return MappingProxyType(body)


def issue_readiness_receipt_v2(
    *,
    kind: str,
    context: ReadinessValidationContext,
    evidence: Mapping[str, Any],
    captured_at: datetime,
    expires_at: datetime,
    now: datetime | None = None,
    max_age_seconds: int,
) -> Mapping[str, Any]:
    """Issue one deterministic v2 receipt after semantic validation."""

    if kind not in PREACTIVATION_KINDS:
        raise CapturedPaperReadinessEvidenceError("readiness kind is unsupported")
    captured = _utc(captured_at, "captured_at")
    expires = _utc(expires_at, "expires_at")
    evidence_body = json.loads(canonical_json_bytes(evidence).decode("utf-8"))
    body: dict[str, Any] = {
        "schema_version": f"{READINESS_SCHEMA_PREFIX}{kind}.v2",
        "receipt_kind": kind,
        "verdict": "PASS",
        "captured_at": captured.isoformat(),
        "expires_at": expires.isoformat(),
        "activation_generation": context.activation_generation,
        "account_scope": "alpaca:paper",
        "expected_account_id": context.expected_account_id,
        "code_build_sha256": context.code_build_sha256,
        "effective_config_sha256": context.effective_config_sha256,
        "capture_receipt_sha256": context.capture_receipt_sha256,
        "issuer_source_role": EXPECTED_ISSUER_ROLES[kind],
        "issuer_source_sha256": context.source_hashes[EXPECTED_ISSUER_ROLES[kind]],
        "evidence": evidence_body,
        "evidence_sha256": sha256_json(evidence_body),
        "live_cash_authorized": False,
        "orders_submitted": False,
    }
    body["receipt_sha256"] = sha256_json(body)
    validate_readiness_receipt_v2(
        body,
        kind=kind,
        context=context,
        now=now or captured,
        max_age_seconds=max_age_seconds,
    )
    return MappingProxyType(body)


__all__ = [
    "CapturedPaperReadinessEvidenceError",
    "EXPECTED_ISSUER_ROLES",
    "EXPECTED_SOURCE_RECEIPTS",
    "PREACTIVATION_KINDS",
    "READINESS_EVIDENCE_SCHEMA_PREFIX",
    "READINESS_PROBE_ARTIFACT_SCHEMA_PREFIX",
    "READINESS_SCHEMA_PREFIX",
    "REQUIRED_DATABASE_TABLES",
    "REQUIRED_TASKS",
    "ReadinessValidationContext",
    "canonical_json_bytes",
    "build_readiness_probe_artifact",
    "issue_readiness_receipt_v2",
    "issue_readiness_receipt_v3_from_artifacts",
    "sha256_json",
    "validate_readiness_receipt_v2",
    "validate_readiness_receipt_v3",
]
