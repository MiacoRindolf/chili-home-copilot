"""Assemble one fail-closed, offline Alpaca PAPER preactivation envelope.

This tool does not issue readiness receipts.  It accepts only existing,
explicitly hash-pinned evidence, inventories the exact candidate source bytes,
builds the no-order authority envelope, and asks the activation contract to
re-verify the complete result before publishing it by content hash.

It imports no application module and performs no database, broker, provider,
Task Scheduler, process-control, or service I/O.  Missing operational evidence
is a normal fail-closed result: no preactivation manifest is published.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
from importlib import metadata as importlib_metadata
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Sequence
import uuid

from scripts import captured_paper_activation_contract as contract


UTC = timezone.utc
BUILD_REQUEST_SCHEMA_VERSION = "chili.captured-paper-preactivation-build-request.v2"
RUNTIME_ENV_RECEIPT_SCHEMA_VERSION = "chili.captured-paper-runtime-env.v1"
SETTINGS_PROJECTION_SCHEMA_VERSION = "chili.captured-paper-settings-projection.v1"
BUILDER_REPORT_SCHEMA_VERSION = "chili.captured-paper-preactivation-builder-report.v1"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REPARSE_ATTRIBUTE = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
_MAX_JSON_BYTES = 4 * 1024 * 1024
_MAX_SOURCE_BYTES = 64 * 1024 * 1024
_MAX_DEPENDENCY_CLOSURE_FILES = 8_192
_MAX_DEPENDENCY_CLOSURE_BYTES = 512 * 1024 * 1024
# 2026-07-17: 5 minutes starved the post-smoke tail — the no-order smoke
# receipt is clamped to this envelope's expiry (min(verified.expires_at, ...)
# in the service), and the final manifest may not outlive this envelope
# (contract chronology check), so smoke duration ate most of the receipt
# window and finalize -> cutover -> ActivatePaper could not fit in what was
# left.  12 minutes keeps the 10-minute receipt class fully usable through
# the tail and stays under the contract's 15-minute
# _MAX_MANIFEST_AGE_SECONDS cap with slack for clock skew.
_MANIFEST_TTL = timedelta(minutes=12)
_DEPENDENCY_CLOSURE_SCHEMA_VERSION = "chili.captured-paper-dependency-closure.v1"

_RUNTIME_SECRET_KEYS = frozenset(
    {
        "DATABASE_URL",
        "MASSIVE_API_KEY",
        "POLYGON_API_KEY",
        "CHILI_ORTEX_API_KEY",
        "CHILI_ALPACA_API_KEY",
        "CHILI_ALPACA_API_SECRET",
    }
)

_PREACTIVATION_RECEIPT_KINDS = frozenset(
    set(contract._RECEIPT_MAX_AGE_SECONDS) - {"no_order_smoke"}
)

_SOURCE_RELATIVE_PATHS: Mapping[str, str] = MappingProxyType(
    {
        "activation_contract": "scripts/captured_paper_activation_contract.py",
        "activation_launcher": "scripts/start-captured-alpaca-paper.ps1",
        "activation_service": "scripts/captured_alpaca_paper_service.py",
        "activation_stage0": "scripts/captured_paper_isolated_stage0.py",
        "adaptive_risk_account_lock": (
            "app/services/trading/momentum_neural/adaptive_risk_account_lock.py"
        ),
        "adaptive_risk_policy": "app/services/trading/momentum_neural/adaptive_risk_policy.py",
        "adaptive_risk_request_builder": (
            "app/services/trading/momentum_neural/adaptive_risk_request_builder.py"
        ),
        "adaptive_risk_reservation": (
            "app/services/trading/momentum_neural/adaptive_risk_reservation.py"
        ),
        "adaptive_risk_runtime_contract": (
            "app/services/trading/momentum_neural/adaptive_risk_runtime_contract.py"
        ),
        "alpaca_fill_activity": "app/services/trading/momentum_neural/alpaca_fill_activity.py",
        "alpaca_fill_read_capability": (
            "app/services/trading/momentum_neural/alpaca_fill_read_capability.py"
        ),
        "alpaca_paper_adapter": "app/services/trading/venue/alpaca_spot.py",
        "app_config": "app/config.py",
        "app_db": "app/db.py",
        "app_migrations": "app/migrations.py",
        "auto_arm": "app/services/trading/momentum_neural/auto_arm.py",
        "captured_adaptive_risk_source": (
            "app/services/trading/momentum_neural/captured_adaptive_risk_source.py"
        ),
        "captured_alpaca_paper_adapter": (
            "app/services/trading/momentum_neural/captured_alpaca_paper_adapter.py"
        ),
        "captured_paper_admission": (
            "app/services/trading/momentum_neural/captured_paper_admission.py"
        ),
        "captured_paper_dispatcher": (
            "app/services/trading/momentum_neural/captured_paper_dispatcher.py"
        ),
        "captured_paper_entry_intent": (
            "app/services/trading/momentum_neural/captured_paper_entry_intent.py"
        ),
        "captured_paper_fill_capture": (
            "app/services/trading/momentum_neural/captured_paper_fill_capture.py"
        ),
        "captured_paper_fill_watch": (
            "app/services/trading/momentum_neural/captured_paper_fill_watch.py"
        ),
        "captured_paper_financial_breaker": (
            "app/services/trading/momentum_neural/captured_paper_financial_breaker.py"
        ),
        "captured_paper_initial_admission": (
            "app/services/trading/momentum_neural/captured_paper_initial_admission.py"
        ),
        "captured_paper_initial_candidate_reader": (
            "app/services/trading/momentum_neural/captured_paper_initial_candidate_reader.py"
        ),
        "captured_paper_initial_controller": (
            "app/services/trading/momentum_neural/captured_paper_initial_controller.py"
        ),
        "captured_paper_initial_provider": (
            "app/services/trading/momentum_neural/captured_paper_initial_provider.py"
        ),
        "captured_paper_initial_recovery": (
            "app/services/trading/momentum_neural/captured_paper_initial_recovery.py"
        ),
        "captured_paper_iqfeed_trigger": (
            "app/services/trading/momentum_neural/captured_paper_iqfeed_trigger.py"
        ),
        "captured_paper_host_cutover": "scripts/captured_paper_host_cutover.py",
        "captured_paper_preactivation_probes": (
            "scripts/run_captured_paper_preactivation_probes.py"
        ),
        "captured_paper_lifecycle_preflight": (
            "scripts/run_captured_paper_lifecycle_preflight.py"
        ),
        "captured_paper_pytest_side_effect_guard": (
            "scripts/captured_paper_pytest_side_effect_guard.py"
        ),
        "captured_paper_outbox": "app/services/trading/momentum_neural/captured_paper_outbox.py",
        "captured_paper_phase_one_handoff": (
            "app/services/trading/momentum_neural/captured_paper_phase_one_handoff.py"
        ),
        "captured_paper_pending_owner": (
            "app/services/trading/momentum_neural/captured_paper_pending_owner.py"
        ),
        "captured_paper_positive_acceptance": (
            "app/services/trading/momentum_neural/captured_paper_positive_acceptance.py"
        ),
        "captured_paper_preowner_promotion": (
            "app/services/trading/momentum_neural/captured_paper_preowner_promotion.py"
        ),
        "captured_paper_post_commit_worker": (
            "app/services/trading/momentum_neural/captured_paper_post_commit_worker.py"
        ),
        "captured_paper_production_material": (
            "app/services/trading/momentum_neural/captured_paper_production_material.py"
        ),
        "captured_paper_production_provider": (
            "app/services/trading/momentum_neural/captured_paper_production_provider.py"
        ),
        "captured_paper_restart_inventory": (
            "app/services/trading/momentum_neural/captured_paper_restart_inventory.py"
        ),
        "captured_paper_selection": (
            "app/services/trading/momentum_neural/captured_paper_selection.py"
        ),
        "captured_paper_selection_frontier_model": (
            "app/models/captured_paper_selection_frontier.py"
        ),
        "captured_paper_selection_producer": (
            "app/services/trading/momentum_neural/captured_paper_selection_producer.py"
        ),
        "captured_paper_selection_queue": (
            "app/services/trading/momentum_neural/captured_paper_selection_queue.py"
        ),
        "captured_paper_selection_runtime": (
            "app/services/trading/momentum_neural/captured_paper_selection_runtime.py"
        ),
        "captured_paper_selection_source": (
            "app/services/trading/momentum_neural/captured_paper_selection_source.py"
        ),
        "captured_paper_service_supervisor": (
            "app/services/trading/momentum_neural/captured_paper_service_supervisor.py"
        ),
        "captured_paper_service_fence": (
            "app/services/trading/momentum_neural/captured_paper_service_fence.py"
        ),
        "captured_paper_transport": (
            "app/services/trading/momentum_neural/captured_paper_transport_coordinator.py"
        ),
        "captured_paper_transport_worker": (
            "app/services/trading/momentum_neural/captured_paper_transport_worker.py"
        ),
        "captured_paper_variant_binding": (
            "app/services/trading/momentum_neural/captured_paper_variant_binding.py"
        ),
        "captured_viability_adapter": (
            "app/services/trading/momentum_neural/captured_viability_adapter.py"
        ),
        "entry_gates": "app/services/trading/momentum_neural/entry_gates.py",
        "execution_family_registry": "app/services/trading/execution_family_registry.py",
        "first_dip_tape_decision": (
            "app/services/trading/momentum_neural/first_dip_tape_decision.py"
        ),
        "first_dip_tape_policy": "app/services/trading/momentum_neural/first_dip_tape_policy.py",
        "iqfeed_capture_bootstrap": "scripts/iqfeed_capture_bootstrap.py",
        "iqfeed_capture_bootstrap_preflight": "scripts/iqfeed_capture_bootstrap_preflight.py",
        "iqfeed_capture_host": "scripts/iqfeed_capture_host.py",
        "iqfeed_depth_bridge": "scripts/iqfeed_depth_bridge.py",
        "iqfeed_l1_capture": "app/services/trading/momentum_neural/iqfeed_l1_capture.py",
        "iqfeed_l2_capture": "app/services/trading/momentum_neural/iqfeed_l2_capture.py",
        "iqfeed_trade_bridge": "scripts/iqfeed_trade_bridge.py",
        "live_replay_capture": "app/services/trading/momentum_neural/live_replay_capture.py",
        "live_runner": "app/services/trading/momentum_neural/live_runner.py",
        "live_runner_loop": "app/services/trading/momentum_neural/live_runner_loop.py",
        "momentum_viability": (
            "app/services/trading/momentum_neural/viability.py"
        ),
        "replay_capture_contract": (
            "app/services/trading/momentum_neural/replay_capture_contract.py"
        ),
        "replay_capture_runtime": "app/services/trading/momentum_neural/replay_capture_runtime.py",
        "readiness_evidence": "scripts/captured_paper_readiness_evidence.py",
        "runtime_environment": "scripts/captured_paper_runtime_env.py",
        "trading_models": "app/models/trading.py",
        "yf_session": "app/services/yf_session.py",
    }
)

_RUNTIME_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "source_path",
        "source_sha256",
        "expected_account_id",
        "first_dip_policy_mode",
        "effective_config",
        "secret_fingerprints",
        "removed_forbidden_keys",
        "configuration_sha256",
    }
)
_SETTINGS_PROJECTION_KEYS = frozenset(
    {
        "schema_version",
        "runtime_environment_sha256",
        "settings",
        "adaptive_risk_policy",
        "captured_paper_operational_policy",
        "captured_paper_config_isolated",
        "paper_credentials_present",
        "live_cash_credentials_present",
        "cash_broker_environment_keys_present",
        "settings_projection_sha256",
    }
)
_REQUEST_KEYS = frozenset(
    {
        "schema_version",
        "activation_generation",
        "expected_account_id",
        "candidate_root",
        "capture_store_root",
        "runtime_environment_receipt",
        "settings_projection",
        "capture_binding",
        "iqfeed_bootstrap",
        "launcher_arguments",
        "readiness_receipts",
        "cutover",
    }
)
_REFERENCE_FIELDS = (
    "runtime_environment_receipt",
    "settings_projection",
    "capture_binding",
    "iqfeed_bootstrap",
    "launcher_arguments",
)


class CapturedPaperPreactivationBuildError(RuntimeError):
    """Stable rejection from the local-only preactivation assembler."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        missing_evidence: Iterable[str] = (),
    ) -> None:
        self.code = str(code)
        self.message = str(message)
        self.missing_evidence = tuple(sorted(set(map(str, missing_evidence))))
        super().__init__(f"{self.code}: {self.message}")


@dataclass(frozen=True, slots=True)
class CapturedPaperCodeInventory:
    candidate_root: Path
    artifacts: tuple[Mapping[str, str], ...]
    source_paths: Mapping[str, Path]
    source_hashes: Mapping[str, str]
    code_build_sha256: str

    @property
    def code_build(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "schema_version": contract.CODE_BUILD_SCHEMA_VERSION,
                "artifacts": [dict(row) for row in self.artifacts],
                "code_build_sha256": self.code_build_sha256,
            }
        )


@dataclass(frozen=True, slots=True)
class BuiltCapturedPaperPreactivation:
    manifest_path: Path
    manifest_sha256: str
    request_path: Path
    request_sha256: str
    code_inventory: CapturedPaperCodeInventory
    evidence_hashes: Mapping[str, str]
    verified: contract.VerifiedCapturedPaperPreactivation


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
        raise CapturedPaperPreactivationBuildError(
            "NON_CANONICAL_JSON", "preactivation input is not canonical JSON"
        ) from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _sha(value: Any, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if _SHA256_RE.fullmatch(normalized) is None:
        raise CapturedPaperPreactivationBuildError(
            "INVALID_SHA256", f"{field} is not a lowercase SHA-256"
        )
    return normalized


def _uuid(value: Any, field: str) -> str:
    normalized = str(value or "").strip().lower()
    try:
        parsed = uuid.UUID(normalized)
    except (AttributeError, TypeError, ValueError) as exc:
        raise CapturedPaperPreactivationBuildError(
            "INVALID_UUID", f"{field} is not a canonical UUID"
        ) from exc
    if str(parsed) != normalized:
        raise CapturedPaperPreactivationBuildError(
            "INVALID_UUID", f"{field} is not a canonical UUID"
        )
    return normalized


def _aware_utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CapturedPaperPreactivationBuildError(
            "INVALID_CLOCK", f"{field} is not timezone-aware"
        )
    return value.astimezone(UTC)


def _parse_utc(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise CapturedPaperPreactivationBuildError(
            "INVALID_CLOCK", f"{field} is not a timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CapturedPaperPreactivationBuildError(
            "INVALID_CLOCK", f"{field} is not a timestamp"
        ) from exc
    return _aware_utc(parsed, field)


def _iso(value: datetime) -> str:
    return _aware_utc(value, "timestamp").isoformat().replace("+00:00", "Z")


def _reject_reparse_chain(path: Path) -> None:
    cursor = path
    while True:
        info = os.lstat(cursor)
        attrs = int(getattr(info, "st_file_attributes", 0) or 0)
        if stat.S_ISLNK(info.st_mode) or attrs & _REPARSE_ATTRIBUTE:
            raise CapturedPaperPreactivationBuildError(
                "REPARSE_PATH_REJECTED", f"path traverses a reparse point: {path}"
            )
        parent = cursor.parent
        if parent == cursor:
            return
        cursor = parent


def _local_absolute(value: str | Path, field: str, *, strict: bool = True) -> Path:
    path = Path(value)
    if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
        raise CapturedPaperPreactivationBuildError(
            "NONLOCAL_PATH", f"{field} must be an absolute local path"
        )
    try:
        resolved = path.resolve(strict=strict)
    except OSError as exc:
        raise CapturedPaperPreactivationBuildError(
            "PATH_UNAVAILABLE", f"{field} is unavailable"
        ) from exc
    if os.name == "nt":
        import ctypes

        anchor = str(resolved.anchor or "")
        if anchor and int(ctypes.windll.kernel32.GetDriveTypeW(anchor)) == 4:
            raise CapturedPaperPreactivationBuildError(
                "NETWORK_DRIVE_REJECTED", f"{field} may not use a network drive"
            )
    _reject_reparse_chain(resolved if strict else resolved.parent)
    return resolved


def _roots(values: Sequence[str | Path]) -> tuple[Path, ...]:
    roots: list[Path] = []
    for index, value in enumerate(values):
        root = _local_absolute(value, f"allowed_read_roots[{index}]")
        if not root.is_dir():
            raise CapturedPaperPreactivationBuildError(
                "INVALID_ROOT", "allowed read root is not a directory"
            )
        if root not in roots:
            roots.append(root)
    if not roots:
        raise CapturedPaperPreactivationBuildError(
            "INVALID_ROOT", "at least one allowed read root is required"
        )
    return tuple(roots)


def _inside(path: Path, roots: Sequence[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _stable_read(
    value: str | Path,
    *,
    expected_sha256: str | None,
    roots: Sequence[Path],
    field: str,
    max_bytes: int = _MAX_JSON_BYTES,
    allow_empty: bool = False,
) -> tuple[Path, bytes, str]:
    path = _local_absolute(value, field)
    if not path.is_file() or not _inside(path, roots):
        raise CapturedPaperPreactivationBuildError(
            "PATH_OUTSIDE_ROOT", f"{field} escaped the allowed roots"
        )
    before = os.stat(path, follow_symlinks=False)
    if (
        not stat.S_ISREG(before.st_mode)
        or (before.st_size <= 0 and not allow_empty)
        or before.st_size > max_bytes
    ):
        raise CapturedPaperPreactivationBuildError(
            "INVALID_FILE", f"{field} is not a bounded nonempty regular file"
        )
    chunks: list[bytes] = []
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise CapturedPaperPreactivationBuildError(
                    "INVALID_FILE", f"{field} grew beyond its bounded size"
                )
            digest.update(chunk)
            chunks.append(chunk)
    after = os.stat(path, follow_symlinks=False)
    if (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or total != after.st_size
    ):
        raise CapturedPaperPreactivationBuildError(
            "FILE_CHANGED", f"{field} changed while being read"
        )
    actual = digest.hexdigest()
    if expected_sha256 is not None and actual != _sha(expected_sha256, field):
        raise CapturedPaperPreactivationBuildError(
            "HASH_MISMATCH", f"{field} content hash mismatch"
        )
    return path, b"".join(chunks), actual


def _strict_json(raw: bytes, field: str) -> Mapping[str, Any]:
    def pairs(rows: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in rows:
            if key in result:
                raise CapturedPaperPreactivationBuildError(
                    "DUPLICATE_JSON_KEY", f"{field} repeats key {key}"
                )
            result[key] = item
        return result

    def constant(value: str) -> Any:
        raise CapturedPaperPreactivationBuildError(
            "NONFINITE_JSON", f"{field} contains {value}"
        )

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CapturedPaperPreactivationBuildError(
            "INVALID_JSON", f"{field} is not strict UTF-8 JSON"
        ) from exc
    if not isinstance(value, Mapping):
        raise CapturedPaperPreactivationBuildError(
            "INVALID_JSON", f"{field} root is not an object"
        )
    return value


def _exact_keys(value: Mapping[str, Any], expected: Iterable[str], field: str) -> None:
    wanted = set(expected)
    actual = set(value)
    if actual != wanted:
        raise CapturedPaperPreactivationBuildError(
            "SCHEMA_MISMATCH",
            f"{field} keys differ; missing={sorted(wanted-actual)} extra={sorted(actual-wanted)}",
        )


def _reference(
    value: Any,
    *,
    roots: Sequence[Path],
    field: str,
    json_required: bool = True,
) -> tuple[Path, bytes, str, Mapping[str, Any] | None]:
    if not isinstance(value, Mapping):
        raise CapturedPaperPreactivationBuildError(
            "INVALID_REFERENCE", f"{field} is not an artifact reference"
        )
    _exact_keys(value, {"path", "sha256"}, field)
    path, raw, digest = _stable_read(
        value.get("path"),
        expected_sha256=value.get("sha256"),
        roots=roots,
        field=field,
        max_bytes=_MAX_JSON_BYTES if json_required else _MAX_SOURCE_BYTES,
    )
    document = _strict_json(raw, field) if json_required else None
    return path, raw, digest, document


def inventory_captured_paper_code(
    candidate_root: str | Path,
    *,
    allowed_read_roots: Sequence[str | Path],
) -> CapturedPaperCodeInventory:
    """Hash primary PAPER entrypoints plus their complete local import closure."""

    roots = _roots(allowed_read_roots)
    root = _local_absolute(candidate_root, "candidate_root")
    if not root.is_dir() or not _inside(root, roots):
        raise CapturedPaperPreactivationBuildError(
            "INVALID_CANDIDATE_ROOT", "candidate root escaped the allowed roots"
        )
    if set(_SOURCE_RELATIVE_PATHS) != set(contract._REQUIRED_CODE_ROLES):
        raise CapturedPaperPreactivationBuildError(
            "CODE_ROSTER_MISMATCH",
            "builder source map differs from the activation contract",
        )
    rows: list[Mapping[str, str]] = []
    paths: dict[str, Path] = {}
    hashes: dict[str, str] = {}
    primary_path_roles: dict[Path, str] = {}
    for role, relative in sorted(_SOURCE_RELATIVE_PATHS.items()):
        path, _raw, digest = _stable_read(
            root / relative,
            expected_sha256=None,
            roots=(root,),
            field=f"code_build.{role}",
            max_bytes=_MAX_SOURCE_BYTES,
        )
        paths[role] = path
        hashes[role] = digest
        primary_path_roles[path] = role
        rows.append(MappingProxyType({"role": role, "path": str(path), "sha256": digest}))
    try:
        dependency_closure = contract.discover_captured_paper_local_dependency_closure(
            candidate_root=root,
            seed_paths=tuple(primary_path_roles),
        )
    except contract.CapturedPaperActivationContractError as exc:
        raise CapturedPaperPreactivationBuildError(exc.code, exc.message) from exc
    for module_name, dependency_path in dependency_closure.items():
        if dependency_path in primary_path_roles:
            continue
        role = contract.dependency_role(module_name)
        path, _raw, digest = _stable_read(
            dependency_path,
            expected_sha256=None,
            roots=(root,),
            field=f"code_build.{role}",
            max_bytes=_MAX_SOURCE_BYTES,
            allow_empty=True,
        )
        paths[role] = path
        hashes[role] = digest
        rows.append(
            MappingProxyType({"role": role, "path": str(path), "sha256": digest})
        )
    rows.sort(key=lambda row: row["role"])
    paths = {role: paths[role] for role in sorted(paths)}
    hashes = {role: hashes[role] for role in sorted(hashes)}
    body = {
        "schema_version": contract.CODE_BUILD_SCHEMA_VERSION,
        "artifacts": [dict(row) for row in rows],
    }
    return CapturedPaperCodeInventory(
        candidate_root=root,
        artifacts=tuple(rows),
        source_paths=MappingProxyType(paths),
        source_hashes=MappingProxyType(hashes),
        code_build_sha256=contract.sha256_json(body),
    )


def _normalized_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", str(value).strip()).lower()


def _captured_paper_external_import_roots(
    inventory: CapturedPaperCodeInventory,
) -> tuple[str, ...]:
    """Derive imports executed while composing the sealed PAPER runtime.

    Function-local optional product integrations are deliberately not bundled:
    they are not reached by the PAPER service composition and the sealed finder
    rejects them if that assumption ever changes.  Module-scope imports are
    followed recursively through the exact local roster.  Known dynamic lane
    imports are explicit seeds so AST string indirection cannot hide them.
    """

    def module_name(path: Path) -> str | None:
        try:
            relative = path.relative_to(inventory.candidate_root)
        except ValueError:
            return None
        if path.suffix.casefold() != ".py":
            return None
        parts = list(relative.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts.pop()
        if not parts or any(not part.isidentifier() for part in parts):
            return None
        return ".".join(parts)

    module_paths: dict[str, Path] = {}
    for path in inventory.source_paths.values():
        name = module_name(path)
        if name:
            module_paths[name] = path
    excluded_roles = {
        "activation_launcher",
        "activation_stage0",
        "captured_paper_preactivation_probes",
        "captured_paper_lifecycle_preflight",
        "captured_paper_pytest_side_effect_guard",
        "iqfeed_capture_bootstrap",
        "iqfeed_capture_bootstrap_preflight",
        "iqfeed_capture_host",
        "iqfeed_depth_bridge",
        "iqfeed_l1_capture",
        "iqfeed_l2_capture",
        "iqfeed_trade_bridge",
    }
    pending: list[str] = []
    for role, path in inventory.source_paths.items():
        if role.startswith("local_dependency:") or role in excluded_roles:
            continue
        name = module_name(path)
        if name:
            pending.append(name)
    # 2026-07-17: idinagdag ang ta/pandas/pytz/sqlalchemy — ang AST walker ay
    # hindi umabot sa module-scope imports ng app/services/trading/scanner.py
    # (naranasan live: "unsealed import rejected: ta" sa no-order boot).
    external: set[str] = {
        "alpaca",
        "pandas",
        "psutil",
        "psycopg2",
        "pytz",
        "sqlalchemy",
        "ta",
        "zstandard",
    }
    visited: set[str] = set()

    while pending:
        current = pending.pop()
        if current in visited:
            continue
        path = module_paths.get(current)
        if path is None:
            continue
        visited.add(current)
        try:
            tree = ast.parse(path.read_bytes(), filename=str(path))
        except (OSError, SyntaxError, ValueError) as exc:
            raise CapturedPaperPreactivationBuildError(
                "PYTHON_DEPENDENCY_CLOSURE_INVALID",
                f"cannot derive dependency imports from {path.name}",
            ) from exc
        discovered: list[ast.Import | ast.ImportFrom] = []

        class ModuleImportVisitor(ast.NodeVisitor):
            optional_depth = 0

            def visit_FunctionDef(self, _node: ast.FunctionDef) -> None:
                return None

            def visit_AsyncFunctionDef(self, _node: ast.AsyncFunctionDef) -> None:
                return None

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                # Class bodies execute during module import.  Descend through
                # them while the FunctionDef overrides below still exclude
                # imports that are lazy inside methods.
                self.generic_visit(node)

            def visit_Import(self, node: ast.Import) -> None:
                if self.optional_depth == 0:
                    discovered.append(node)

            def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
                if self.optional_depth == 0:
                    discovered.append(node)

            def visit_Try(self, node: ast.Try) -> None:
                optional = any(
                    handler.type is None
                    or (
                        isinstance(handler.type, ast.Name)
                        and handler.type.id
                        in {"ImportError", "ModuleNotFoundError", "Exception"}
                    )
                    for handler in node.handlers
                )
                if optional:
                    self.optional_depth += 1
                for child in node.body:
                    self.visit(child)
                if optional:
                    self.optional_depth -= 1
                for collection in (node.handlers, node.orelse, node.finalbody):
                    for child in collection:
                        self.visit(child)

            def visit_If(self, node: ast.If) -> None:
                type_checking = (
                    isinstance(node.test, ast.Name)
                    and node.test.id == "TYPE_CHECKING"
                ) or (
                    isinstance(node.test, ast.Attribute)
                    and node.test.attr == "TYPE_CHECKING"
                )
                if type_checking:
                    for child in node.orelse:
                        self.visit(child)
                    return
                self.generic_visit(node)

        ModuleImportVisitor().visit(tree)
        package_parts = current.split(".")
        if path.name != "__init__.py":
            package_parts.pop()
        for node in discovered:
            candidates: list[str] = []
            if isinstance(node, ast.Import):
                candidates.extend(alias.name for alias in node.names)
            else:
                if node.level:
                    base = list(package_parts)
                    if node.level > 1:
                        base = base[: -(node.level - 1)]
                    if node.module:
                        base.extend(node.module.split("."))
                    resolved = ".".join(base)
                else:
                    resolved = str(node.module or "")
                if resolved:
                    candidates.append(resolved)
                    candidates.extend(
                        f"{resolved}.{alias.name}"
                        for alias in node.names
                        if alias.name != "*"
                    )
            for name in candidates:
                if name in module_paths:
                    pending.append(name)
                    continue
                top = name.partition(".")[0]
                if top and top not in {"app", "scripts"} and top not in sys.stdlib_module_names:
                    external.add(top)
    return tuple(sorted(external))


def _dependency_distribution_closure(
    *, source_root: Path, module_seeds: Sequence[str]
) -> tuple[tuple[importlib_metadata.Distribution, ...], tuple[str, ...]]:
    """Resolve installed distributions and active base requirements only."""

    distributions = tuple(importlib_metadata.distributions(path=[str(source_root)]))
    if not distributions:
        return (), tuple(module_seeds)
    by_name: dict[str, importlib_metadata.Distribution] = {}
    by_module: dict[str, set[str]] = {}
    for distribution in distributions:
        raw_name = distribution.metadata.get("Name")
        if not raw_name:
            continue
        name = _normalized_distribution_name(raw_name)
        if name in by_name:
            raise CapturedPaperPreactivationBuildError(
                "PYTHON_DEPENDENCY_CLOSURE_INVALID",
                f"duplicate installed distribution identity {name}",
            )
        by_name[name] = distribution
        candidates: set[str] = set()
        top_level = distribution.read_text("top_level.txt")
        if top_level:
            candidates.update(
                line.strip()
                for line in top_level.splitlines()
                if line.strip() and line.strip().isidentifier()
            )
        for item in distribution.files or ():
            parts = Path(str(item)).parts
            if not parts:
                continue
            first = parts[0]
            if first.endswith((".dist-info", ".data")):
                continue
            module = first[:-3] if first.endswith(".py") else first
            if module.isidentifier():
                candidates.add(module)
        for module in candidates:
            by_module.setdefault(module, set()).add(name)

    requested: list[str] = []
    unresolved: list[str] = []
    for module in module_seeds:
        owners = sorted(by_module.get(module, ()))
        if len(owners) != 1:
            unresolved.append(module)
        else:
            requested.append(owners[0])
    if unresolved:
        raise CapturedPaperPreactivationBuildError(
            "PYTHON_DEPENDENCY_MODULE_UNRESOLVED",
            "dependency modules are missing or ambiguous: " + ",".join(unresolved),
        )

    try:
        from packaging.requirements import Requirement
    except ImportError as exc:
        raise CapturedPaperPreactivationBuildError(
            "PYTHON_DEPENDENCY_CLOSURE_INVALID",
            "packaging is required to evaluate installed requirement markers",
        ) from exc
    selected: set[str] = set()
    pending = list(requested)
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        distribution = by_name.get(name)
        if distribution is None:
            raise CapturedPaperPreactivationBuildError(
                "PYTHON_DEPENDENCY_DISTRIBUTION_MISSING",
                f"required installed distribution is unavailable: {name}",
            )
        selected.add(name)
        for raw_requirement in distribution.metadata.get_all("Requires-Dist") or ():
            try:
                requirement = Requirement(raw_requirement)
                active = requirement.marker is None or requirement.marker.evaluate(
                    {"extra": ""}
                )
            except (TypeError, ValueError) as exc:
                raise CapturedPaperPreactivationBuildError(
                    "PYTHON_DEPENDENCY_CLOSURE_INVALID",
                    f"invalid installed requirement metadata for {name}",
                ) from exc
            if active:
                pending.append(_normalized_distribution_name(requirement.name))
    return tuple(by_name[name] for name in sorted(selected)), ()


def _stage_captured_paper_dependency_capsule(
    *,
    source_root: Path,
    generation_root: Path,
    inventory: CapturedPaperCodeInventory,
    python_executable: Path,
    python_executable_sha256: str,
) -> tuple[Path, str]:
    """Copy the deterministic reachable distribution closure into one capsule."""

    module_seeds = _captured_paper_external_import_roots(inventory)
    distributions, synthetic_modules = _dependency_distribution_closure(
        source_root=source_root,
        module_seeds=module_seeds,
    )
    sources: dict[str, Path] = {}
    distribution_rows: list[Mapping[str, str]] = []
    if distributions:
        for distribution in distributions:
            name = _normalized_distribution_name(
                str(distribution.metadata.get("Name") or "")
            )
            version = str(distribution.version or "")
            distribution_rows.append(
                MappingProxyType({"name": name, "version": version})
            )
            for item in distribution.files or ():
                candidate = Path(distribution.locate_file(item))
                try:
                    resolved = candidate.resolve(strict=True)
                    relative = resolved.relative_to(source_root).as_posix()
                except (OSError, RuntimeError, ValueError):
                    # Console entrypoints outside site-packages are not import
                    # dependencies and are intentionally outside the capsule.
                    continue
                if (
                    "__pycache__" in tuple(part.casefold() for part in Path(relative).parts)
                    or relative.casefold().endswith((".pyc", ".pyo"))
                ):
                    continue
                metadata = os.lstat(resolved)
                if not stat.S_ISREG(metadata.st_mode) or (
                    int(getattr(metadata, "st_file_attributes", 0))
                    & _REPARSE_ATTRIBUTE
                ):
                    raise CapturedPaperPreactivationBuildError(
                        "PYTHON_DEPENDENCY_CLOSURE_INVALID",
                        f"distribution file is not a regular non-reparse file: {relative}",
                    )
                existing = sources.get(relative)
                if existing is not None and existing != resolved:
                    raise CapturedPaperPreactivationBuildError(
                        "PYTHON_DEPENDENCY_CLOSURE_COLLISION",
                        f"multiple distributions claim {relative}",
                    )
                sources[relative] = resolved
    else:
        # Tiny synthetic roots used by hermetic tests have no dist-info.  A
        # bounded all-file capsule remains exact and fail-closed; the same hard
        # budget prevents this path from silently accepting a real full env.
        for candidate in source_root.rglob("*"):
            if not candidate.is_file():
                continue
            relative = candidate.relative_to(source_root).as_posix()
            if (
                "__pycache__" in tuple(part.casefold() for part in Path(relative).parts)
                or relative.casefold().endswith((".pyc", ".pyo"))
            ):
                continue
            _reject_reparse_chain(candidate)
            sources[relative] = candidate
        distribution_rows.append(
            MappingProxyType({"name": "synthetic-bounded-root", "version": "0"})
        )

    total_bytes = sum(path.stat().st_size for path in sources.values())
    if (
        len(sources) + 1 > _MAX_DEPENDENCY_CLOSURE_FILES
        or total_bytes > _MAX_DEPENDENCY_CLOSURE_BYTES
    ):
        raise CapturedPaperPreactivationBuildError(
            "PYTHON_DEPENDENCY_RESOURCE_BUDGET_EXCEEDED",
            "dependency closure exceeds the manifest-bound activation budget",
        )
    dependencies_root = generation_root / "dependencies"
    dependencies_root.mkdir(mode=0o700, exist_ok=True)
    _reject_reparse_chain(dependencies_root)
    pending_parent = Path(
        tempfile.mkdtemp(prefix=".pending-", dir=str(dependencies_root))
    )
    pending_root = pending_parent / "site-packages"
    pending_root.mkdir(mode=0o700)
    provenance_rows: list[Mapping[str, Any]] = []
    copied_total_bytes = 0
    try:
        for relative in sorted(sources, key=lambda value: (value.casefold(), value)):
            source, raw, digest = _stable_read(
                sources[relative],
                expected_sha256=None,
                roots=(source_root,),
                field=f"python_dependency_closure.{relative}",
                max_bytes=_MAX_SOURCE_BYTES,
                allow_empty=True,
            )
            del source
            target = pending_root.joinpath(*Path(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as handle:
                handle.write(raw)
            copied_total_bytes += len(raw)
            if copied_total_bytes > _MAX_DEPENDENCY_CLOSURE_BYTES:
                raise CapturedPaperPreactivationBuildError(
                    "PYTHON_DEPENDENCY_RESOURCE_BUDGET_EXCEEDED",
                    "dependency bytes drifted beyond the activation budget",
                )
            provenance_rows.append(
                MappingProxyType(
                    {
                        "path": relative,
                        "sha256": digest,
                        "size_bytes": len(raw),
                    }
                )
            )
        provenance = {
            "schema_version": _DEPENDENCY_CLOSURE_SCHEMA_VERSION,
            "distributions": [dict(row) for row in distribution_rows],
            "files": [dict(row) for row in provenance_rows],
            "module_seeds": list(module_seeds or synthetic_modules),
            "resource_budget": {
                "max_files": _MAX_DEPENDENCY_CLOSURE_FILES,
                "max_total_bytes": _MAX_DEPENDENCY_CLOSURE_BYTES,
            },
        }
        provenance_raw = _canonical_json_bytes(provenance)
        if copied_total_bytes + len(provenance_raw) > _MAX_DEPENDENCY_CLOSURE_BYTES:
            raise CapturedPaperPreactivationBuildError(
                "PYTHON_DEPENDENCY_RESOURCE_BUDGET_EXCEEDED",
                "dependency closure plus provenance exceeds the activation budget",
            )
        provenance_path = pending_root / "_chili_dependency_closure.json"
        with provenance_path.open("xb") as handle:
            handle.write(provenance_raw)
            handle.flush()
            os.fsync(handle.fileno())
        pending_identity = contract.python_dependency_root_identity(
            dependency_root=pending_root,
            python_executable=python_executable,
            python_executable_sha256=python_executable_sha256,
        )
        tree_sha = str(pending_identity["tree_sha256"])
        final_parent = dependencies_root / tree_sha
        final_root = final_parent / "site-packages"
        if final_parent.exists():
            shutil.rmtree(pending_parent)
        else:
            os.replace(pending_parent, final_parent)
        final_identity_sha = contract.python_dependency_root_identity_sha256(
            dependency_root=final_root,
            python_executable=python_executable,
            python_executable_sha256=python_executable_sha256,
        )
        if final_root.parent.name.casefold() != tree_sha:
            raise CapturedPaperPreactivationBuildError(
                "PYTHON_DEPENDENCY_CAPSULE_INVALID",
                "dependency capsule is not addressed by its exact tree hash",
            )
        return final_root, final_identity_sha
    except BaseException:
        if pending_parent.exists():
            shutil.rmtree(pending_parent, ignore_errors=True)
        raise


def build_launcher_argument_contract_offline(
    *,
    activation_generation: str,
    activation_artifact_root: str | Path,
    candidate_root: str | Path,
    python_executable: str | Path,
    python_dependency_root: str | Path,
    allowed_read_roots: Sequence[str | Path],
    no_order_receipt_output: str | Path,
) -> Mapping[str, Any]:
    """Stage immutable entrypoint bytes and build the three-mode contract."""

    roots = _roots(allowed_read_roots)
    inventory = inventory_captured_paper_code(
        candidate_root,
        allowed_read_roots=roots,
    )
    generation = _uuid(activation_generation, "activation_generation")
    artifact_root = _local_absolute(
        activation_artifact_root, "activation_artifact_root"
    )
    if not artifact_root.is_dir() or not _inside(artifact_root, roots):
        raise CapturedPaperPreactivationBuildError(
            "ACTIVATION_ARTIFACT_ROOT_INVALID",
            "activation artifact root escaped the allowed local roots",
        )

    generation_root = artifact_root / generation
    generation_root.mkdir(mode=0o700, exist_ok=True)
    _reject_reparse_chain(generation_root)

    def stage(role: str, suffix: str) -> Path:
        source_path, raw, source_sha = _stable_read(
            inventory.source_paths[role],
            expected_sha256=inventory.source_hashes[role],
            roots=(inventory.candidate_root,),
            field=f"activation_artifacts.{role}.source",
            max_bytes=_MAX_SOURCE_BYTES,
        )
        del source_path
        directory = generation_root / source_sha
        directory.mkdir(mode=0o700, exist_ok=True)
        _reject_reparse_chain(directory)
        target = directory / f"{source_sha}{suffix}"
        try:
            with target.open("xb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            pass
        staged, staged_raw, staged_sha = _stable_read(
            target,
            expected_sha256=source_sha,
            roots=(artifact_root,),
            field=f"activation_artifacts.{role}.staged",
            max_bytes=_MAX_SOURCE_BYTES,
        )
        if staged_raw != raw or staged_sha != source_sha:
            raise CapturedPaperPreactivationBuildError(
                "ACTIVATION_ARTIFACT_COLLISION",
                f"staged {role} bytes differ from their source",
            )
        return staged

    staged_launcher = stage("activation_launcher", ".ps1")
    staged_service = stage("activation_service", ".py")
    staged_stage0 = stage("activation_stage0", ".py")
    handshake_root = generation_root / "handshake"
    handshake_root.mkdir(mode=0o700, exist_ok=True)
    _reject_reparse_chain(handshake_root)
    host_ready_receipt = handshake_root / "host-ready.json"
    host_ready_paths = (
        host_ready_receipt,
        host_ready_receipt.with_name(host_ready_receipt.name + ".permit.json"),
        host_ready_receipt.with_name(host_ready_receipt.name + ".started.json"),
        host_ready_receipt.with_name(
            host_ready_receipt.name + ".revocation-requested.json"
        ),
        host_ready_receipt.with_name(host_ready_receipt.name + ".revoked.json"),
        host_ready_receipt.with_name(host_ready_receipt.name + ".dispatch.lock"),
    )
    if any(os.path.lexists(path) for path in host_ready_paths):
        raise CapturedPaperPreactivationBuildError(
            "HOST_READY_RECEIPT_ALREADY_EXISTS",
            "generation-owned host-ready receipt paths must all be new",
        )
    python_path, _python_raw, python_sha = _stable_read(
        python_executable,
        expected_sha256=None,
        roots=roots,
        field="launcher_arguments.python_executable",
        max_bytes=_MAX_SOURCE_BYTES,
    )
    dependency_source_root = _local_absolute(
        python_dependency_root, "launcher_arguments.python_dependency_root"
    )
    if not dependency_source_root.is_dir() or not _inside(dependency_source_root, roots):
        raise CapturedPaperPreactivationBuildError(
            "PYTHON_DEPENDENCY_ROOT_INVALID",
            "Python dependency root escaped allowed roots",
        )
    _reject_reparse_chain(dependency_source_root)
    dependency_root, dependency_identity_sha = _stage_captured_paper_dependency_capsule(
        source_root=dependency_source_root,
        generation_root=generation_root,
        inventory=inventory,
        python_executable=python_path,
        python_executable_sha256=python_sha,
    )
    try:
        receipt_path = contract._strict_local_output_path(
            no_order_receipt_output,
            roots=roots,
            field="launcher_arguments.no_order_receipt_output_path",
        )
    except contract.CapturedPaperActivationContractError as exc:
        raise CapturedPaperPreactivationBuildError(exc.code, exc.message) from exc
    if os.path.lexists(receipt_path):
        raise CapturedPaperPreactivationBuildError(
            "OUTPUT_ALREADY_EXISTS",
            "no-order receipt output must be new for this activation generation",
        )

    invocations: dict[str, Mapping[str, Any]] = {}
    for mode in sorted(contract._LAUNCHER_MODE_BINDINGS):
        try:
            projection = contract.launcher_invocation_projection(
                mode=mode,
                candidate_root=inventory.candidate_root,
                python_executable=python_path,
                python_executable_sha256=python_sha,
                python_dependency_root=dependency_root,
                python_dependency_root_identity_sha256=dependency_identity_sha,
                allowed_read_roots=roots,
                launcher_path=inventory.source_paths["activation_launcher"],
                launcher_sha256=inventory.source_hashes["activation_launcher"],
                stage0_path=inventory.source_paths["activation_stage0"],
                stage0_sha256=inventory.source_hashes["activation_stage0"],
                service_path=inventory.source_paths["activation_service"],
                service_sha256=inventory.source_hashes["activation_service"],
                launcher_staged_path=staged_launcher,
                stage0_staged_path=staged_stage0,
                service_staged_path=staged_service,
                host_ready_receipt=(
                    host_ready_receipt if mode == "ActivatePaper" else None
                ),
                no_order_receipt_output=(
                    receipt_path if mode == "NoOrderSmoke" else None
                ),
            )
        except contract.CapturedPaperActivationContractError as exc:
            raise CapturedPaperPreactivationBuildError(exc.code, exc.message) from exc
        invocations[mode] = {
            "projection": dict(projection),
            "projection_sha256": contract.sha256_json(projection),
        }
    return {
        "schema_version": contract.LAUNCHER_ARGUMENT_CONTRACT_SCHEMA_VERSION,
        "invocations": invocations,
    }


def _validate_runtime_evidence(
    *,
    runtime_document: Mapping[str, Any],
    projection_document: Mapping[str, Any],
    expected_account_id: str,
    roots: Sequence[Path],
) -> tuple[dict[str, str], str]:
    _exact_keys(runtime_document, _RUNTIME_RECEIPT_KEYS, "runtime_environment_receipt")
    if runtime_document.get("schema_version") != RUNTIME_ENV_RECEIPT_SCHEMA_VERSION:
        raise CapturedPaperPreactivationBuildError(
            "RUNTIME_EVIDENCE_INVALID", "runtime environment receipt schema is unsupported"
        )
    if (
        _uuid(runtime_document.get("expected_account_id"), "runtime.expected_account_id")
        != expected_account_id
        or runtime_document.get("first_dip_policy_mode") != "candidate"
    ):
        raise CapturedPaperPreactivationBuildError(
            "RUNTIME_EVIDENCE_INVALID", "runtime environment escaped candidate PAPER identity"
        )
    effective = runtime_document.get("effective_config")
    fingerprints = runtime_document.get("secret_fingerprints")
    removed = runtime_document.get("removed_forbidden_keys")
    if (
        not isinstance(effective, Mapping)
        or not effective
        or not isinstance(fingerprints, Mapping)
        or not {
            "DATABASE_URL",
            "CHILI_ALPACA_API_KEY",
            "CHILI_ALPACA_API_SECRET",
        }.issubset(fingerprints)
        or any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or _SHA256_RE.fullmatch(value) is None
            for key, value in fingerprints.items()
        )
        or not isinstance(removed, list)
        or any(not isinstance(value, str) or not value for value in removed)
        or removed != sorted(set(removed))
        or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in effective.items()
        )
        or not set(fingerprints).issubset(_RUNTIME_SECRET_KEYS)
        or bool(set(effective) & _RUNTIME_SECRET_KEYS)
    ):
        raise CapturedPaperPreactivationBuildError(
            "RUNTIME_EVIDENCE_INVALID", "runtime environment evidence is incomplete"
        )
    if (
        effective.get("CHILI_CAPTURED_PAPER_CONFIG_ISOLATED") != "true"
        or effective.get("CHILI_ALPACA_ENABLED") != "true"
        or effective.get("CHILI_ALPACA_PAPER") != "true"
        or effective.get("CHILI_ALPACA_EXPECTED_ACCOUNT_ID") != expected_account_id
        or effective.get("CHILI_EQUITY_EXECUTION_RAIL") != "alpaca"
        or effective.get("CHILI_MOMENTUM_EQUITY_EXECUTION_VIA_ALPACA_PAPER")
        != "true"
        or effective.get("CHILI_MOMENTUM_CRYPTO_EXECUTION_VIA_ALPACA_PAPER")
        != "false"
        or effective.get("CHILI_MOMENTUM_FIRST_DIP_RECLAIM_POLICY_MODE")
        != "candidate"
        or effective.get("CHILI_MOMENTUM_SHORT_ENABLED") != "false"
        or effective.get("CHILI_MOMENTUM_SHORT_LANE_ENABLED") != "false"
        or re.fullmatch(
            r"[1-9][0-9]*",
            str(effective.get("CHILI_AUTOTRADER_USER_ID") or ""),
        )
        is None
        or int(effective["CHILI_AUTOTRADER_USER_ID"]) > 2_147_483_647
        or any(
            key.startswith(("CHILI_ALPACA_LIVE_", "ALPACA_LIVE_", "ROBINHOOD_", "COINBASE_"))
            for key in effective
        )
    ):
        raise CapturedPaperPreactivationBuildError(
            "RUNTIME_EVIDENCE_INVALID", "runtime environment contains forbidden authority"
        )
    source_path, _source_raw, source_sha = _stable_read(
        runtime_document.get("source_path"),
        expected_sha256=runtime_document.get("source_sha256"),
        roots=roots,
        field="runtime_environment.source_env",
        max_bytes=_MAX_SOURCE_BYTES,
    )
    config_body = {
        "schema_version": runtime_document["schema_version"],
        "source_path": str(source_path),
        "source_sha256": source_sha,
        "expected_account_id": expected_account_id,
        "first_dip_policy_mode": "candidate",
        "effective_config": dict(effective),
        "secret_fingerprints": dict(fingerprints),
    }
    configuration_sha = _sha(
        runtime_document.get("configuration_sha256"), "runtime.configuration_sha256"
    )
    if _sha256_json(config_body) != configuration_sha:
        raise CapturedPaperPreactivationBuildError(
            "RUNTIME_EVIDENCE_INVALID", "runtime environment receipt digest mismatch"
        )

    _exact_keys(projection_document, _SETTINGS_PROJECTION_KEYS, "settings_projection")
    if projection_document.get("schema_version") != SETTINGS_PROJECTION_SCHEMA_VERSION:
        raise CapturedPaperPreactivationBuildError(
            "SETTINGS_PROJECTION_INVALID", "settings projection schema is unsupported"
        )
    projection_body = dict(projection_document)
    claimed_projection_sha = _sha(
        projection_body.pop("settings_projection_sha256", None),
        "settings_projection_sha256",
    )
    settings = projection_document.get("settings")
    if (
        _sha256_json(projection_body) != claimed_projection_sha
        or projection_document.get("runtime_environment_sha256") != configuration_sha
        or projection_document.get("captured_paper_config_isolated") is not True
        or projection_document.get("paper_credentials_present") is not True
        or projection_document.get("live_cash_credentials_present") is not False
        or projection_document.get("cash_broker_environment_keys_present") is not False
        or not isinstance(settings, Mapping)
        or not isinstance(projection_document.get("adaptive_risk_policy"), Mapping)
        or not projection_document.get("adaptive_risk_policy")
        or not isinstance(projection_document.get("captured_paper_operational_policy"), Mapping)
        or not projection_document.get("captured_paper_operational_policy")
    ):
        raise CapturedPaperPreactivationBuildError(
            "SETTINGS_PROJECTION_INVALID", "settings projection failed its sealed posture"
        )
    expected_settings = {
        "chili_alpaca_enabled": True,
        "chili_alpaca_paper": True,
        "chili_alpaca_expected_account_id": expected_account_id,
        "chili_equity_execution_rail": "alpaca",
        "chili_momentum_auto_arm_equity_only": True,
        "chili_momentum_auto_arm_crypto_only": False,
        "chili_momentum_first_dip_reclaim_policy_mode": "candidate",
        "chili_momentum_short_enabled": False,
        "chili_momentum_short_lane_enabled": False,
        "chili_autotrader_user_id": int(effective["CHILI_AUTOTRADER_USER_ID"]),
    }
    if any(settings.get(key) != value for key, value in expected_settings.items()):
        raise CapturedPaperPreactivationBuildError(
            "SETTINGS_PROJECTION_INVALID", "settings projection escaped PAPER/equity policy"
        )
    return (
        {
            "source_env_path": str(source_path),
            "source_env_sha256": source_sha,
            "runtime_environment_sha256": configuration_sha,
            "effective_config_sha256": claimed_projection_sha,
            "database_target_fingerprint": _sha(
                fingerprints.get("DATABASE_URL"), "database_target_fingerprint"
            ),
        },
        claimed_projection_sha,
    )


def _missing_evidence(request: Mapping[str, Any]) -> tuple[str, ...]:
    missing: list[str] = []
    for field in _REFERENCE_FIELDS:
        if not isinstance(request.get(field), Mapping):
            missing.append(field)
    receipts = request.get("readiness_receipts")
    if not isinstance(receipts, Mapping):
        missing.extend(f"readiness_receipts.{kind}" for kind in _PREACTIVATION_RECEIPT_KINDS)
    else:
        missing.extend(
            f"readiness_receipts.{kind}"
            for kind in _PREACTIVATION_RECEIPT_KINDS
            if not isinstance(receipts.get(kind), Mapping)
        )
    return tuple(sorted(set(missing)))


def _publish_content_addressed(
    root: Path, raw: bytes
) -> tuple[Path, str, bool]:
    digest = hashlib.sha256(raw).hexdigest()
    parent = root / digest[:2]
    parent.mkdir(parents=True, exist_ok=True)
    _reject_reparse_chain(parent)
    path = parent / f"{digest}.json"
    created = False
    try:
        with path.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        created = True
    except FileExistsError:
        existing_path, existing, existing_sha = _stable_read(
            path,
            expected_sha256=digest,
            roots=(root,),
            field="published_preactivation",
        )
        if existing_path != path or existing_sha != digest or existing != raw:
            raise CapturedPaperPreactivationBuildError(
                "CONTENT_ADDRESS_COLLISION", "preactivation object path contains different bytes"
            )
    return path, digest, created


def build_captured_paper_preactivation_offline(
    *,
    request_path: str | Path,
    request_sha256: str,
    candidate_root: str | Path,
    output_root: str | Path,
    allowed_read_roots: Sequence[str | Path],
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> BuiltCapturedPaperPreactivation:
    """Bind existing evidence and publish a verified no-order envelope.

    No readiness/capture/runtime PASS artifact is created here.  All such
    artifacts must exist before this function is called and must carry their
    exact expected content hashes in the build request.
    """

    roots = _roots(allowed_read_roots)
    now = _aware_utc(wall_clock(), "wall_clock")
    request_file, request_raw, request_digest = _stable_read(
        request_path,
        expected_sha256=request_sha256,
        roots=roots,
        field="build_request",
    )
    request = _strict_json(request_raw, "build_request")
    extras = set(request) - set(_REQUEST_KEYS)
    if extras:
        raise CapturedPaperPreactivationBuildError(
            "SCHEMA_MISMATCH", f"build request contains extra keys: {sorted(extras)}"
        )
    missing_fields = set(_REQUEST_KEYS) - set(request)
    missing_evidence = _missing_evidence(request)
    if missing_evidence:
        raise CapturedPaperPreactivationBuildError(
            "MISSING_EVIDENCE",
            "required operational evidence is absent",
            missing_evidence=missing_evidence,
        )
    if missing_fields:
        raise CapturedPaperPreactivationBuildError(
            "SCHEMA_MISMATCH", f"build request is missing fields: {sorted(missing_fields)}"
        )
    if request.get("schema_version") != BUILD_REQUEST_SCHEMA_VERSION:
        raise CapturedPaperPreactivationBuildError(
            "REQUEST_SCHEMA_MISMATCH", "preactivation build request schema is unsupported"
        )

    generation = _uuid(request.get("activation_generation"), "activation_generation")
    account_id = _uuid(request.get("expected_account_id"), "expected_account_id")
    candidate = _local_absolute(candidate_root, "candidate_root")
    declared_candidate = _local_absolute(request.get("candidate_root"), "request.candidate_root")
    if candidate != declared_candidate or not candidate.is_dir() or not _inside(candidate, roots):
        raise CapturedPaperPreactivationBuildError(
            "CANDIDATE_ROOT_MISMATCH", "request and operator candidate roots differ"
        )
    capture_store = _local_absolute(request.get("capture_store_root"), "capture_store_root")
    destination = _local_absolute(output_root, "output_root")
    if (
        not capture_store.is_dir()
        or not destination.is_dir()
        or not _inside(capture_store, roots)
        or not _inside(destination, roots)
    ):
        raise CapturedPaperPreactivationBuildError(
            "PATH_OUTSIDE_ROOT", "capture/output root escaped the allowed roots"
        )

    inventory = inventory_captured_paper_code(candidate, allowed_read_roots=roots)
    evidence_hashes: dict[str, str] = {}
    documents: dict[str, Mapping[str, Any]] = {}
    raw_documents: dict[str, bytes] = {}
    paths: dict[str, Path] = {}
    for field in _REFERENCE_FIELDS:
        path, raw, digest, document = _reference(
            request[field],
            roots=roots,
            field=field,
            json_required=True,
        )
        paths[field] = path
        evidence_hashes[field] = digest
        raw_documents[field] = raw
        if document is not None:
            documents[field] = document

    try:
        launcher_invocations = contract._validate_launcher_argument_contract(
            documents["launcher_arguments"],
            raw=raw_documents["launcher_arguments"],
            candidate_root=candidate,
            allowed_read_roots=roots,
            source_paths=inventory.source_paths,
            source_hashes=inventory.source_hashes,
            activation_generation=generation,
        )
    except contract.CapturedPaperActivationContractError as exc:
        raise CapturedPaperPreactivationBuildError(exc.code, exc.message) from exc
    no_order_output = Path(
        str(
            launcher_invocations["NoOrderSmoke"].get(
                "no_order_receipt_output_path"
            )
            or ""
        )
    )
    if os.path.lexists(no_order_output):
        raise CapturedPaperPreactivationBuildError(
            "OUTPUT_ALREADY_EXISTS",
            "no-order receipt output must be new for this activation generation",
        )

    runtime_fields, effective_config_sha = _validate_runtime_evidence(
        runtime_document=documents["runtime_environment_receipt"],
        projection_document=documents["settings_projection"],
        expected_account_id=account_id,
        roots=roots,
    )

    receipt_refs = request["readiness_receipts"]
    if set(receipt_refs) != set(_PREACTIVATION_RECEIPT_KINDS):
        raise CapturedPaperPreactivationBuildError(
            "READINESS_ROSTER_MISMATCH", "readiness receipt roster is not exact"
        )
    manifest_expiry = now + _MANIFEST_TTL
    normalized_receipts: dict[str, dict[str, str]] = {}
    for kind in sorted(_PREACTIVATION_RECEIPT_KINDS):
        path, _raw, digest, receipt = _reference(
            receipt_refs[kind], roots=roots, field=f"readiness_receipts.{kind}"
        )
        assert receipt is not None
        expires_at = _parse_utc(receipt.get("expires_at"), f"{kind}.expires_at")
        # ENVELOPE POLICY (2026-07-17 bug fix): every readiness receipt is
        # freshness-gated at its OWN probe boundary; inheriting min(receipt
        # expiries) here produced envelopes that expired before the producing
        # flow could even return them (observed live: the 60s
        # capture_host_smoke receipt left a 41s window that ended 4s BEFORE
        # the manifest file hit disk — structurally unusable).  A receipt
        # already expired at build time still rejects the build below; the
        # envelope itself is governed by _MANIFEST_TTL (contract-capped at
        # 15 minutes), which bounds the smoke -> finalize handoff.
        if expires_at <= now:
            raise CapturedPaperPreactivationBuildError(
                "EVIDENCE_STALE", f"{kind} receipt is already expired"
            )
        normalized_receipts[kind] = {"path": str(path), "sha256": digest}
        evidence_hashes[f"readiness_receipts.{kind}"] = digest
    if manifest_expiry <= now:
        raise CapturedPaperPreactivationBuildError(
            "EVIDENCE_STALE", "at least one required receipt is already expired"
        )

    cutover = request.get("cutover")
    if not isinstance(cutover, Mapping):
        raise CapturedPaperPreactivationBuildError(
            "CUTOVER_INVALID", "cutover binding is not an object"
        )
    _exact_keys(cutover, {"scheduled_tasks", "singleton_policy", "rollback_required"}, "cutover")
    tasks = cutover.get("scheduled_tasks")
    if (
        not isinstance(tasks, list)
        or len(tasks) != len(set(tasks))
        or set(tasks) != set(contract._REQUIRED_TASKS)
        or cutover.get("singleton_policy") != "one_unified_candidate_host"
        or cutover.get("rollback_required") is not True
    ):
        raise CapturedPaperPreactivationBuildError(
            "CUTOVER_INVALID", "cutover did not bind the exact rollback-safe host plan"
        )
    activate_projection = launcher_invocations["ActivatePaper"]

    document: dict[str, Any] = {
        "schema_version": contract.PREACTIVATION_MANIFEST_SCHEMA_VERSION,
        "generated_at": _iso(now),
        "expires_at": _iso(manifest_expiry),
        "activation_generation": generation,
        "authority_boundary": {
            "broker": "alpaca",
            "broker_environment": "paper",
            "account_scope": "alpaca:paper",
            "expected_account_id": account_id,
            "equity_long_only": True,
            "first_dip_policy_mode": "candidate",
            "paper_order_submission_authorized": False,
            "live_cash_authorized": False,
            "short_authorized": False,
            "crypto_authorized": False,
            "real_money_authorized": False,
        },
        "runtime_environment": runtime_fields,
        "code_build": dict(inventory.code_build),
        "capture_binding": {
            "path": str(paths["capture_binding"]),
            "sha256": evidence_hashes["capture_binding"],
        },
        "iqfeed_bootstrap": {
            "path": str(paths["iqfeed_bootstrap"]),
            "sha256": evidence_hashes["iqfeed_bootstrap"],
        },
        "readiness_receipts": normalized_receipts,
        "cutover": {
            "candidate_root": str(candidate),
            "activation_artifact_root": str(
                Path(str(activate_projection["launcher_path"])).parent.parent.parent
            ),
            "launcher_source_path": str(
                activate_projection["launcher_source_path"]
            ),
            "launcher_source_sha256": str(
                activate_projection["launcher_source_sha256"]
            ),
            "launcher_path": str(activate_projection["launcher_path"]),
            "launcher_sha256": inventory.source_hashes["activation_launcher"],
            "stage0_source_path": str(activate_projection["stage0_source_path"]),
            "stage0_source_sha256": str(
                activate_projection["stage0_source_sha256"]
            ),
            "stage0_path": str(activate_projection["stage0_path"]),
            "stage0_sha256": str(activate_projection["stage0_sha256"]),
            "service_source_path": str(activate_projection["service_source_path"]),
            "service_source_sha256": str(
                activate_projection["service_source_sha256"]
            ),
            "service_path": str(activate_projection["service_staged_path"]),
            "service_sha256": str(activate_projection["service_sha256"]),
            "host_ready_receipt_base": str(
                activate_projection["host_ready_receipt_base"]
            ),
            "python_import_root": str(activate_projection["python_import_root"]),
            "launcher_arguments_path": str(paths["launcher_arguments"]),
            "launcher_arguments_sha256": evidence_hashes["launcher_arguments"],
            "python_executable_path": str(
                activate_projection["python_executable_path"]
            ),
            "python_executable_sha256": str(
                activate_projection["python_executable_sha256"]
            ),
            "python_dependency_root": str(
                activate_projection["python_dependency_root"]
            ),
            "python_dependency_root_identity_sha256": str(
                activate_projection["python_dependency_root_identity_sha256"]
            ),
            "scheduled_tasks": sorted(tasks),
            "singleton_policy": "one_unified_candidate_host",
            "rollback_required": True,
        },
        "capture_store_root": str(capture_store),
    }
    document["activation_manifest_sha256"] = contract.sha256_json(document)
    raw = _canonical_json_bytes(document)

    # Verify the complete object before placing it in the content-addressed
    # namespace.  The pending file is always removed, including on rejection.
    pending = destination / f".pending-preactivation-{uuid.uuid4()}.json"
    try:
        with pending.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        pending_sha = hashlib.sha256(raw).hexdigest()
        try:
            contract.load_captured_paper_preactivation(
                pending,
                expected_manifest_sha256=pending_sha,
                candidate_root=candidate,
                allowed_read_roots=roots,
                wall_clock=lambda: now,
            )
        except contract.CapturedPaperActivationContractError as exc:
            raise CapturedPaperPreactivationBuildError(exc.code, exc.message) from exc
    finally:
        try:
            pending.unlink(missing_ok=True)
        except OSError:
            pass

    final_path, final_sha, final_created = _publish_content_addressed(
        destination, raw
    )
    try:
        verified = contract.load_captured_paper_preactivation(
            final_path,
            expected_manifest_sha256=final_sha,
            candidate_root=candidate,
            allowed_read_roots=roots,
            wall_clock=lambda: now,
        )
        if verified.effective_config_sha256 != effective_config_sha:
            raise CapturedPaperPreactivationBuildError(
                "SETTINGS_PROJECTION_INVALID",
                "published envelope changed settings projection",
            )
    except (
        contract.CapturedPaperActivationContractError,
        CapturedPaperPreactivationBuildError,
    ) as exc:
        if final_created:
            try:
                final_path.unlink()
            except OSError as cleanup_exc:
                raise CapturedPaperPreactivationBuildError(
                    "REJECTED_ARTIFACT_CLEANUP_FAILED",
                    "rejected preactivation artifact could not be removed",
                ) from cleanup_exc
        if isinstance(exc, contract.CapturedPaperActivationContractError):
            raise CapturedPaperPreactivationBuildError(exc.code, exc.message) from exc
        raise
    return BuiltCapturedPaperPreactivation(
        manifest_path=final_path,
        manifest_sha256=final_sha,
        request_path=request_file,
        request_sha256=request_digest,
        code_inventory=inventory,
        evidence_hashes=MappingProxyType(evidence_hashes),
        verified=verified,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--request-sha256", required=True)
    parser.add_argument("--candidate-root", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--allow-read-root", action="append", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        built = build_captured_paper_preactivation_offline(
            request_path=args.request,
            request_sha256=args.request_sha256,
            candidate_root=args.candidate_root,
            output_root=args.output_root,
            allowed_read_roots=tuple(args.allow_read_root),
        )
        report: dict[str, Any] = {
            "schema_version": BUILDER_REPORT_SCHEMA_VERSION,
            "verdict": "CAPTURED_ALPACA_PAPER_PREACTIVATION_PUBLISHED",
            "manifest_path": str(built.manifest_path),
            "manifest_sha256": built.manifest_sha256,
            "request_sha256": built.request_sha256,
            "code_build_sha256": built.code_inventory.code_build_sha256,
            "bound_evidence_sha256": dict(sorted(built.evidence_hashes.items())),
            "missing_evidence": [],
            "offline_tooling_only": True,
            "preactivation_published": True,
            "paper_order_submission_authorized": False,
            "paper_service_started": False,
            "orders_submitted": False,
            "live_cash_authorized": False,
        }
        code = 0
    except (CapturedPaperPreactivationBuildError, OSError, ValueError) as exc:
        report = {
            "schema_version": BUILDER_REPORT_SCHEMA_VERSION,
            "verdict": "CAPTURED_ALPACA_PAPER_PREACTIVATION_REJECTED",
            "error_code": str(getattr(exc, "code", "OFFLINE_PREACTIVATION_REJECTED")),
            "missing_evidence": list(getattr(exc, "missing_evidence", ())),
            "offline_tooling_only": True,
            "preactivation_published": False,
            "paper_order_submission_authorized": False,
            "paper_service_started": False,
            "orders_submitted": False,
            "live_cash_authorized": False,
        }
        code = 2
    sys.stdout.buffer.write(_canonical_json_bytes(report) + b"\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BUILD_REQUEST_SCHEMA_VERSION",
    "BUILDER_REPORT_SCHEMA_VERSION",
    "BuiltCapturedPaperPreactivation",
    "CapturedPaperCodeInventory",
    "CapturedPaperPreactivationBuildError",
    "build_launcher_argument_contract_offline",
    "build_captured_paper_preactivation_offline",
    "inventory_captured_paper_code",
    "main",
]
