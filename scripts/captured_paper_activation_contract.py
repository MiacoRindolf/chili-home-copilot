"""Offline verifier for one prospective Alpaca PAPER activation envelope.

The envelope is the final, externally pinned authorization input consumed by
the dedicated captured-paper service.  Loading it performs only bounded local
file reads.  It never imports application settings, opens a database, contacts
IQFeed/Alpaca, changes a scheduled task, or submits an order.

This contract deliberately separates strategy policy from operational
authority.  The intended adaptive candidate policy is hash-bound, while the
ability to contact Alpaca PAPER is granted only after fresh account,
kill-switch, lifecycle, capture-host, no-order-smoke, and rollback receipts all
bind the same generation.  Live cash, shorts, and crypto are structurally
outside the accepted authority boundary.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Sequence
import uuid

from scripts import captured_paper_readiness_evidence as readiness_evidence


UTC = timezone.utc
PREACTIVATION_MANIFEST_SCHEMA_VERSION = "chili.captured-paper-preactivation.v2"
ACTIVATION_MANIFEST_SCHEMA_VERSION = "chili.captured-paper-activation.v3"
CODE_BUILD_SCHEMA_VERSION = "chili.captured-paper-code-build.v1"
PYTHON_DEPENDENCY_ROOT_IDENTITY_SCHEMA_VERSION = (
    "chili.captured-paper-python-dependency-root-identity.v2"
)
RECEIPT_SCHEMA_PREFIX = "chili.captured-paper-readiness."
CAPTURE_BINDING_SCHEMA_VERSION = "chili.captured-paper-capture-binding.v1"
LAUNCHER_ARGUMENT_CONTRACT_SCHEMA_VERSION = (
    "chili.captured-paper-launcher-argument-contract.v1"
)
LAUNCHER_INVOCATION_PROJECTION_SCHEMA_VERSION = (
    "chili.captured-paper-launcher-invocation-projection.v1"
)
LAUNCHER_MANIFEST_PATH_TOKEN = "@verified:content-addressed-manifest-path"
LAUNCHER_MANIFEST_SHA256_TOKEN = "@verified:manifest-file-sha256"
LAUNCHER_SINGLETON_NAME = "Global\\CHILI-Captured-Alpaca-PAPER-SINGLETON"
IQFEED_BOOTSTRAP_MANIFEST_SCHEMA_VERSION = (
    "chili.iqfeed-capture-bootstrap-preflight.v2"
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REPARSE_ATTRIBUTE = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_MAX_MANIFEST_AGE_SECONDS = 20 * 60
_MAX_FUTURE_SKEW_SECONDS = 5
_DEPENDENCY_ROLE_PREFIX = "local_dependency:"

_LAUNCHER_MODE_BINDINGS: Mapping[str, tuple[str, str, str]] = MappingProxyType(
    {
        "ActivatePaper": (
            "activate-paper",
            ACTIVATION_MANIFEST_SCHEMA_VERSION,
            "forbidden",
        ),
        "NoOrderSmoke": (
            "no-order-smoke",
            PREACTIVATION_MANIFEST_SCHEMA_VERSION,
            "required",
        ),
        "ValidateOnly": (
            "validate-only",
            ACTIVATION_MANIFEST_SCHEMA_VERSION,
            "forbidden",
        ),
    }
)

_REQUIRED_TASKS = frozenset(
    {
        "CHILI-IQFeed-Depth-Bridge-Daily",
        "CHILI-IQFeed-Depth-Bridge-Logon",
        "CHILI-IQFeed-Trade-Bridge-Daily",
        "CHILI-IQFeed-Trade-Bridge-Logon",
    }
)

_REQUIRED_CODE_ROLES = frozenset(
    {
        "activation_contract",
        "activation_launcher",
        "activation_service",
        "activation_stage0",
        "adaptive_risk_account_lock",
        "adaptive_risk_policy",
        "adaptive_risk_request_builder",
        "adaptive_risk_reservation",
        "adaptive_risk_runtime_contract",
        "alpaca_fill_activity",
        "alpaca_fill_read_capability",
        "alpaca_paper_adapter",
        "app_config",
        "app_db",
        "app_migrations",
        "auto_arm",
        "captured_adaptive_risk_source",
        "captured_alpaca_paper_adapter",
        "captured_paper_admission",
        "captured_paper_dispatcher",
        "captured_paper_entry_intent",
        "captured_paper_fill_capture",
        "captured_paper_fill_watch",
        "captured_paper_financial_breaker",
        "captured_paper_initial_admission",
        "captured_paper_initial_candidate_reader",
        "captured_paper_initial_controller",
        "captured_paper_initial_provider",
        "captured_paper_initial_recovery",
        "captured_paper_iqfeed_trigger",
        "captured_paper_host_cutover",
        "captured_paper_preactivation_probes",
        "captured_paper_lifecycle_preflight",
        "captured_paper_pytest_side_effect_guard",
        "captured_paper_outbox",
        "captured_paper_phase_one_handoff",
        "captured_paper_pending_owner",
        "captured_paper_positive_acceptance",
        "captured_paper_preowner_promotion",
        "captured_paper_post_commit_worker",
        "captured_paper_production_material",
        "captured_paper_production_provider",
        "captured_paper_restart_inventory",
        "captured_paper_selection",
        "captured_paper_selection_frontier_model",
        "captured_paper_selection_producer",
        "captured_paper_selection_queue",
        "captured_paper_selection_runtime",
        "captured_paper_selection_source",
        "captured_paper_service_fence",
        "captured_paper_service_supervisor",
        "captured_paper_transport",
        "captured_paper_transport_worker",
        "captured_paper_variant_binding",
        "captured_viability_adapter",
        "entry_gates",
        "execution_family_registry",
        "first_dip_tape_decision",
        "first_dip_tape_policy",
        "iqfeed_capture_bootstrap",
        "iqfeed_capture_bootstrap_preflight",
        "iqfeed_capture_host",
        "iqfeed_depth_bridge",
        "iqfeed_l1_capture",
        "iqfeed_l2_capture",
        "iqfeed_trade_bridge",
        "live_replay_capture",
        "live_runner",
        "live_runner_loop",
        "momentum_viability",
        "replay_capture_contract",
        "replay_capture_runtime",
        "readiness_evidence",
        "runtime_environment",
        "trading_models",
        "yf_session",
    }
)


def _module_name_for_local_source(candidate_root: Path, path: Path) -> str:
    """Return the deterministic import name for one local Python source."""

    relative = path.relative_to(candidate_root)
    parts = list(relative.parts)
    if not parts or not parts[-1].casefold().endswith(".py"):
        raise CapturedPaperActivationContractError(
            "LOCAL_DEPENDENCY_INVALID", "local dependency is not Python source"
        )
    leaf = parts.pop()[:-3]
    if leaf != "__init__":
        parts.append(leaf)
    if not parts or any(not str(part).isidentifier() for part in parts):
        raise CapturedPaperActivationContractError(
            "LOCAL_DEPENDENCY_INVALID",
            f"local dependency has no canonical import name: {relative}",
        )
    return ".".join(map(str, parts))


def _local_python_module_index(candidate_root: Path) -> Mapping[str, Path]:
    """Inventory importable local ``app``/``scripts`` modules without importing."""

    result: dict[str, Path] = {}
    for top_level in ("app", "scripts"):
        base = candidate_root / top_level
        if not base.is_dir():
            raise CapturedPaperActivationContractError(
                "LOCAL_DEPENDENCY_INVALID",
                f"candidate is missing local package root {top_level}",
            )
        _reject_reparse_chain(base)
        for path in sorted(base.rglob("*.py"), key=lambda value: str(value).casefold()):
            if "__pycache__" in path.parts:
                continue
            resolved = path.resolve(strict=True)
            _reject_reparse_chain(resolved)
            if not resolved.is_file():
                raise CapturedPaperActivationContractError(
                    "LOCAL_DEPENDENCY_INVALID",
                    f"local module is not a regular file: {resolved}",
                )
            try:
                name = _module_name_for_local_source(candidate_root, resolved)
            except CapturedPaperActivationContractError:
                # Migration snippets such as ``171_name.py`` are data files,
                # not importable Python modules.  A seed/import that points to
                # one still fails below because it cannot resolve in this index.
                continue
            if name in result and result[name] != resolved:
                raise CapturedPaperActivationContractError(
                    "LOCAL_DEPENDENCY_INVALID",
                    f"duplicate local module identity: {name}",
                )
            result[name] = resolved
    return MappingProxyType(result)


def _package_initializers(
    module_name: str,
    module_index: Mapping[str, Path],
) -> tuple[str, ...]:
    parts = module_name.split(".")
    return tuple(
        candidate
        for index in range(1, len(parts))
        if (candidate := ".".join(parts[:index])) in module_index
        and module_index[candidate].name == "__init__.py"
    )


def _resolve_import_targets(
    *,
    tree: ast.AST,
    importer: str,
    importer_is_package: bool,
    module_index: Mapping[str, Path],
) -> tuple[str, ...]:
    """Resolve local static/constant-dynamic imports from parsed source."""

    discovered: set[str] = set()

    def add(candidate: str) -> None:
        candidate = str(candidate or "").strip(".")
        if candidate in module_index:
            discovered.add(candidate)
            discovered.update(_package_initializers(candidate, module_index))

    importer_package = importer if importer_is_package else importer.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add(alias.name)
            continue
        if isinstance(node, ast.ImportFrom):
            if node.level:
                package_parts = importer_package.split(".") if importer_package else []
                trim = node.level - 1
                if trim > len(package_parts):
                    raise CapturedPaperActivationContractError(
                        "LOCAL_DEPENDENCY_INVALID",
                        f"relative import escapes local package in {importer}",
                    )
                base_parts = package_parts[: len(package_parts) - trim]
                if node.module:
                    base_parts.extend(node.module.split("."))
                base = ".".join(base_parts)
            else:
                base = str(node.module or "")
            add(base)
            for alias in node.names:
                if alias.name != "*":
                    add(f"{base}.{alias.name}" if base else alias.name)
            continue
        if not isinstance(node, ast.Call) or not node.args:
            continue
        function_name = ""
        if isinstance(node.func, ast.Name) and node.func.id == "__import__":
            function_name = "__import__"
        elif (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "import_module"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "importlib"
        ):
            function_name = "importlib.import_module"
        if not function_name:
            continue
        argument = node.args[0]
        if not isinstance(argument, ast.Constant) or not isinstance(argument.value, str):
            # A computed target cannot be proven non-local from syntax.  Seal
            # every importable local module rather than silently leaving a
            # runtime escape hatch in the inventory.
            discovered.update(module_index)
            continue
        target = argument.value
        if function_name == "importlib.import_module" and target.startswith("."):
            if len(node.args) < 2 or not isinstance(node.args[1], ast.Constant):
                raise CapturedPaperActivationContractError(
                    "DYNAMIC_IMPORT_UNRESOLVED",
                    f"{importer} contains an unresolved relative dynamic import",
                )
            package = str(node.args[1].value or "")
            target = f"{package}.{target.lstrip('.')}"
        add(target)
    return tuple(sorted(discovered))


def discover_captured_paper_local_dependency_closure(
    *,
    candidate_root: str | Path,
    seed_paths: Iterable[str | Path],
) -> Mapping[str, Path]:
    """Compute the deterministic transitive local import closure for PAPER.

    This is deliberately syntax-only: no candidate application module is
    imported and no settings/database/provider side effect can occur.
    """

    root = Path(candidate_root).resolve(strict=True)
    _reject_reparse_chain(root)
    module_index = _local_python_module_index(root)
    reverse_index = {path: name for name, path in module_index.items()}
    pending: set[str] = set()
    for raw_path in seed_paths:
        path = Path(raw_path).resolve(strict=True)
        if path.suffix.casefold() != ".py":
            continue
        name = reverse_index.get(path)
        if name is None:
            raise CapturedPaperActivationContractError(
                "LOCAL_DEPENDENCY_INVALID",
                f"seed Python source is outside the local module index: {path}",
            )
        pending.add(name)
        pending.update(_package_initializers(name, module_index))

    visited: set[str] = set()
    while pending:
        name = min(pending)
        pending.remove(name)
        if name in visited:
            continue
        path = module_index[name]
        try:
            raw = path.read_bytes()
            tree = ast.parse(raw, filename=str(path))
        except (OSError, SyntaxError, ValueError) as exc:
            raise CapturedPaperActivationContractError(
                "LOCAL_DEPENDENCY_INVALID",
                f"cannot parse local dependency {name}",
            ) from exc
        visited.add(name)
        pending.update(
            set(
                _resolve_import_targets(
                    tree=tree,
                    importer=name,
                    importer_is_package=path.name == "__init__.py",
                    module_index=module_index,
                )
            )
            - visited
        )
    return MappingProxyType({name: module_index[name] for name in sorted(visited)})


def dependency_role(module_name: str) -> str:
    if not module_name or any(not part.isidentifier() for part in module_name.split(".")):
        raise CapturedPaperActivationContractError(
            "LOCAL_DEPENDENCY_INVALID", "local dependency module name is invalid"
        )
    return f"{_DEPENDENCY_ROLE_PREFIX}{module_name}"


def python_dependency_root_identity(
    *,
    dependency_root: Path,
    python_executable: Path,
    python_executable_sha256: str,
) -> Mapping[str, Any]:
    """Bind one explicit site-packages root without importing ``site``.

    The isolated stage-0 recomputes this exact identity for the bounded,
    content-addressed dependency capsule, retains mutation-denying handles on
    Windows, and installs a hash-verifying finder before making the root
    visible to metadata/resource APIs.  Interpreter bytes and the filesystem
    object identity are both pinned; user-site and ``.pth`` discovery are not
    used at any point.
    """

    # Import only the sealed stdlib-only implementation.  Keeping one Merkle
    # algorithm prevents the offline builder and isolated runtime from
    # disagreeing about which dependency bytes were admitted.
    from scripts import captured_paper_isolated_stage0 as isolated_stage0

    try:
        value = isolated_stage0.dependency_root_identity(
            dependency_root=Path(dependency_root),
            python_executable=Path(python_executable),
            python_executable_sha256=_sha(
                python_executable_sha256, "python_executable_sha256"
            ),
        )
    except (isolated_stage0.IsolatedStage0Error, OSError, ValueError) as exc:
        raise CapturedPaperActivationContractError(
            "PYTHON_DEPENDENCY_ROOT_INVALID",
            "Python dependency root content identity could not be proven",
        ) from exc
    if value.get("schema_version") != PYTHON_DEPENDENCY_ROOT_IDENTITY_SCHEMA_VERSION:
        raise CapturedPaperActivationContractError(
            "PYTHON_DEPENDENCY_ROOT_INVALID",
            "Python dependency root identity schema is unsupported",
        )
    return MappingProxyType(dict(value))


def python_dependency_root_identity_sha256(
    *,
    dependency_root: Path,
    python_executable: Path,
    python_executable_sha256: str,
) -> str:
    attestation = getattr(sys, "_captured_paper_isolated_stage0", None)
    if isinstance(attestation, Mapping):
        try:
            root = Path(dependency_root).resolve(strict=True)
            executable = Path(python_executable).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise CapturedPaperActivationContractError(
                "PYTHON_DEPENDENCY_ROOT_INVALID",
                "Python dependency root identity inputs are unavailable",
            ) from exc
        if (
            attestation.get("schema_version")
            == "chili.captured-paper-isolated-stage0.v2"
            and Path(str(attestation.get("dependency_root") or "")) == root
            and Path(str(attestation.get("python_executable_path") or ""))
            == executable
            and attestation.get("python_executable_sha256")
            == _sha(python_executable_sha256, "python_executable_sha256")
        ):
            return _sha(
                attestation.get("dependency_root_identity_sha256"),
                "stage0 dependency root identity",
            )
    return sha256_json(
        dict(python_dependency_root_identity(
            dependency_root=dependency_root,
            python_executable=python_executable,
            python_executable_sha256=python_executable_sha256,
        ))
    )

_RECEIPT_MAX_AGE_SECONDS: Mapping[str, int] = MappingProxyType(
    {
        # 2026-07-17: the 30s/60s max-ages on mid-flow receipts were
        # impossible-by-construction at the consumer boundary (the probe
        # battery + flow tail run for minutes; six consecutive live
        # generations died on this).  The receipts are sealed evidence, not
        # the live gate — the service still live-binds the broker and
        # re-checks the kill switch at its own boot.  The class must cover
        # receipt capture (mid-probe-battery) through the LAST consumer
        # (launcher ValidateOnly / ActivatePaper re-walk the full roster):
        # measured ~8-12 minutes from the shared preactivation capture clock
        # through service startup.  A34 proved that 10 minutes starves both
        # lifecycle_preflight and runtime_settings, which share observed_at.
        # Their bounded 20-minute windows match database/capture.  Unbounded
        # operator waits still fail closed — receipt staleness IS the fence.
        "runtime_settings": 20 * 60,
        "broker_account": 10 * 60,
        "database_schema": 20 * 60,
        "capture_host_smoke": 20 * 60,
        "focused_regressions": 60 * 60,
        "lifecycle_preflight": 20 * 60,
        "kill_switch": 10 * 60,
        "no_order_smoke": 20 * 60,
        "rollback_snapshot": 60 * 60,
    }
)

_REQUIRED_CHECKS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "runtime_settings": frozenset(
            {
                "alpaca_paper",
                "paper_credentials_present",
                "live_credentials_absent",
                "equity_only",
                "short_disabled",
                "crypto_disabled",
                "adaptive_policy_parity",
                "first_dip_candidate",
                "magic_activation_caps_absent",
            }
        ),
        "broker_account": frozenset(
            {
                "paper",
                "status_active",
                "identity_match",
                "flat",
                "no_open_orders",
                "trading_blocked_false",
                "transfers_blocked_false",
                "account_read_fresh",
            }
        ),
        "database_schema": frozenset(
            {
                "migration_exact",
                "idempotent_rehearsal_passed",
                "outbox_schema_present",
                "fill_settlement_schema_present",
                "post_settlement_contradiction_schema_present",
                "production_db_target_match",
            }
        ),
        "capture_host_smoke": frozenset(
            {
                "launcher_hash_match",
                "host_hash_match",
                "trade_bridge_hash_match",
                "depth_bridge_hash_match",
                "l1_bound",
                "l2_lane_fail_closed",
                "capture_store_writable",
                "zero_silent_drops",
                "provider_health_fresh",
            }
        ),
        "focused_regressions": frozenset(
            {
                "compile_passed",
                "targeted_tests_passed",
                "failures_zero",
                "network_calls_zero",
                "live_cash_paths_not_exercised",
            }
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
        "kill_switch": frozenset(
            {"readable", "inactive", "same_account", "fresh"}
        ),
        "no_order_smoke": frozenset(
            {
                "service_started",
                "runtime_registered",
                "paper_account_pinned",
                "provider_capture_healthy",
                "transport_disabled",
                "broker_order_count_unchanged",
                "broker_post_calls_zero",
                "live_cash_authority_absent",
            }
        ),
        "rollback_snapshot": frozenset(
            {
                "four_tasks_captured",
                "task_xml_hashes_bound",
                "legacy_processes_captured",
                "restore_commands_validated",
                "candidate_action_hash_bound",
                "singleton_policy_bound",
            }
        ),
    }
)


class CapturedPaperActivationContractError(RuntimeError):
    """Stable fail-closed rejection before any external side effect."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        self.message = str(message)
        super().__init__(f"{self.code}: {self.message}")


@dataclass(frozen=True, slots=True)
class VerifiedCapturedPaperActivation:
    manifest_path: Path
    manifest_sha256: str
    activation_generation: str
    expected_account_id: str
    code_build_sha256: str
    effective_config_sha256: str
    capture_receipt_sha256: str
    source_paths: Mapping[str, Path]
    source_hashes: Mapping[str, str]
    receipt_paths: Mapping[str, Path]
    receipt_hashes: Mapping[str, str]
    launcher_path: Path
    launcher_sha256: str
    candidate_root: Path
    capture_store_root: Path
    iqfeed_bootstrap_manifest_path: Path
    iqfeed_bootstrap_manifest_sha256: str
    generated_at: datetime
    expires_at: datetime
    manifest: Mapping[str, Any]
    envelope_stage: str
    paper_order_submission_authorized: bool

    @property
    def settings_projection_sha256(self) -> str:
        """Parsed settings digest, distinct from each hot-run config digest."""

        return self.effective_config_sha256

    @property
    def report(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "schema_version": "chili.captured-paper-activation-verification.v1",
                "verdict": (
                    "CAPTURED_ALPACA_PAPER_ACTIVATION_VERIFIED"
                    if self.paper_order_submission_authorized
                    else "CAPTURED_ALPACA_PAPER_PREACTIVATION_VERIFIED"
                ),
                "envelope_stage": self.envelope_stage,
                "manifest_path": str(self.manifest_path),
                "manifest_sha256": self.manifest_sha256,
                "activation_generation": self.activation_generation,
                "account_scope": "alpaca:paper",
                "expected_account_id": self.expected_account_id,
                "code_build_sha256": self.code_build_sha256,
                "effective_config_sha256": self.effective_config_sha256,
                "settings_projection_sha256": self.settings_projection_sha256,
                "capture_receipt_sha256": self.capture_receipt_sha256,
                "source_role_count": len(self.source_paths),
                "readiness_receipts": dict(self.receipt_hashes),
                "launcher_path": str(self.launcher_path),
                "launcher_sha256": self.launcher_sha256,
                "candidate_root": str(self.candidate_root),
                "capture_store_root": str(self.capture_store_root),
                "iqfeed_bootstrap_manifest_path": str(
                    self.iqfeed_bootstrap_manifest_path
                ),
                "iqfeed_bootstrap_manifest_sha256": (
                    self.iqfeed_bootstrap_manifest_sha256
                ),
                "generated_at": _iso(self.generated_at),
                "expires_at": _iso(self.expires_at),
                "live_cash_authorized": False,
                "short_authorized": False,
                "crypto_authorized": False,
                "real_money_authorized": False,
                "paper_order_submission_authorized": (
                    self.paper_order_submission_authorized
                ),
            }
        )


@dataclass(frozen=True, slots=True)
class VerifiedCapturedPaperPreactivation(VerifiedCapturedPaperActivation):
    """Typed no-order authority; it can never authorize a broker POST."""


@dataclass(frozen=True, slots=True)
class BuiltCapturedPaperActivation:
    """Content-addressed result of promoting one verified no-order envelope."""

    manifest_path: Path
    manifest_sha256: str
    preactivation_manifest_sha256: str
    no_order_smoke_sha256: str
    verified: VerifiedCapturedPaperActivation


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
        raise CapturedPaperActivationContractError(
            "NON_CANONICAL_JSON", "activation material is not canonical JSON"
        ) from exc


def sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _sha(value: Any, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if _SHA256_RE.fullmatch(normalized) is None:
        raise CapturedPaperActivationContractError(
            "INVALID_SHA256", f"{field} is not a lowercase SHA-256"
        )
    return normalized


def _uuid(value: Any, field: str) -> str:
    normalized = str(value or "").strip().lower()
    try:
        parsed = uuid.UUID(normalized)
    except (AttributeError, TypeError, ValueError) as exc:
        raise CapturedPaperActivationContractError(
            "INVALID_UUID", f"{field} is not a canonical UUID"
        ) from exc
    if str(parsed) != normalized:
        raise CapturedPaperActivationContractError(
            "INVALID_UUID", f"{field} is not a canonical UUID"
        )
    return normalized


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CapturedPaperActivationContractError(
            "INVALID_OBJECT", f"{field} is not an object"
        )
    return value


def _exact_keys(value: Mapping[str, Any], expected: Iterable[str], field: str) -> None:
    actual = set(value)
    wanted = set(expected)
    if actual != wanted:
        raise CapturedPaperActivationContractError(
            "SCHEMA_MISMATCH",
            f"{field} keys differ; missing={sorted(wanted-actual)} extra={sorted(actual-wanted)}",
        )


def _parse_utc(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise CapturedPaperActivationContractError(
            "INVALID_CLOCK", f"{field} is not an aware timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise CapturedPaperActivationContractError(
            "INVALID_CLOCK", f"{field} is not an aware timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise CapturedPaperActivationContractError(
            "INVALID_CLOCK", f"{field} is not an aware timestamp"
        )
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _is_local_absolute(path: Path) -> bool:
    text = str(path)
    return path.is_absolute() and not text.startswith(("\\\\", "//"))


def _reject_network_drive(path: Path) -> None:
    if os.name != "nt":
        return
    import ctypes

    anchor = str(path.anchor or "")
    if anchor and int(ctypes.windll.kernel32.GetDriveTypeW(anchor)) == 4:
        raise CapturedPaperActivationContractError(
            "NETWORK_DRIVE_REJECTED", "activation material may not use a network drive"
        )


def _reject_reparse_chain(path: Path) -> None:
    cursor = path
    while True:
        try:
            info = os.lstat(cursor)
        except OSError as exc:
            raise CapturedPaperActivationContractError(
                "PATH_UNAVAILABLE", f"activation path is unavailable: {path}"
            ) from exc
        attrs = int(getattr(info, "st_file_attributes", 0) or 0)
        if stat.S_ISLNK(info.st_mode) or attrs & _REPARSE_ATTRIBUTE:
            raise CapturedPaperActivationContractError(
                "REPARSE_PATH", f"activation path traverses a reparse point: {path}"
            )
        parent = cursor.parent
        if parent == cursor:
            return
        cursor = parent


def _roots(values: Sequence[str | Path]) -> tuple[Path, ...]:
    resolved: list[Path] = []
    for value in values:
        path = Path(value)
        if not _is_local_absolute(path):
            raise CapturedPaperActivationContractError(
                "NONLOCAL_ROOT", "activation read roots must be absolute local paths"
            )
        path = path.resolve(strict=True)
        _reject_network_drive(path)
        _reject_reparse_chain(path)
        if not path.is_dir():
            raise CapturedPaperActivationContractError(
                "INVALID_ROOT", "activation read root is not a directory"
            )
        resolved.append(path)
    if not resolved:
        raise CapturedPaperActivationContractError(
            "MISSING_ROOT", "at least one activation read root is required"
        )
    return tuple(dict.fromkeys(resolved))


def _inside(path: Path, roots: Sequence[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _canonical_path_text(path: Path) -> str:
    """Return the Windows-semantic path spelling used by launcher receipts."""

    return os.path.normcase(os.path.normpath(str(path)))


def _canonical_root_texts(roots: Sequence[Path]) -> tuple[str, ...]:
    values = {_canonical_path_text(root) for root in roots}
    return tuple(sorted(values))


def _strict_local_output_path(
    value: Any,
    *,
    roots: Sequence[Path],
    field: str,
) -> Path:
    path = Path(str(value or ""))
    if not _is_local_absolute(path) or not path.name or ":" in path.name:
        raise CapturedPaperActivationContractError(
            "NONLOCAL_PATH", f"{field} must name an absolute local file"
        )
    try:
        parent = path.parent.resolve(strict=True)
    except OSError as exc:
        raise CapturedPaperActivationContractError(
            "PATH_UNAVAILABLE", f"{field} parent is unavailable"
        ) from exc
    _reject_reparse_chain(parent)
    _reject_network_drive(parent)
    if not parent.is_dir() or not _inside(parent, roots):
        raise CapturedPaperActivationContractError(
            "PATH_OUTSIDE_ROOT", f"{field} parent escaped the allowed roots"
        )
    resolved = parent / path.name
    if os.path.lexists(resolved):
        try:
            resolved = resolved.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise CapturedPaperActivationContractError(
                "PATH_UNAVAILABLE", f"{field} existing entry is unavailable"
            ) from exc
        _reject_reparse_chain(resolved)
        if not resolved.is_file():
            raise CapturedPaperActivationContractError(
                "INVALID_FILE", f"{field} does not name a regular file"
            )
    return resolved


def launcher_invocation_projection(
    *,
    mode: str,
    candidate_root: Path,
    python_executable: Path,
    python_executable_sha256: str,
    python_dependency_root: Path,
    python_dependency_root_identity_sha256: str,
    allowed_read_roots: Sequence[Path],
    launcher_path: Path,
    launcher_sha256: str,
    stage0_path: Path,
    stage0_sha256: str,
    service_path: Path,
    service_sha256: str,
    no_order_receipt_output: Path | None,
    launcher_staged_path: Path | None = None,
    stage0_staged_path: Path | None = None,
    service_staged_path: Path | None = None,
    host_ready_receipt: Path | None = None,
) -> Mapping[str, Any]:
    """Build the canonical, non-self-referential launcher invocation body.

    The manifest path and outer content SHA cannot literally be committed by a
    field inside that same manifest.  Those two arguments are therefore
    represented by fixed tokens *only after* the launcher independently proves
    the supplied manifest is content addressed and byte-matches its supplied
    SHA.  Every other launch argument is literal and hash-bound.
    """

    binding = _LAUNCHER_MODE_BINDINGS.get(str(mode))
    if binding is None:
        raise CapturedPaperActivationContractError(
            "LAUNCHER_MODE_INVALID", "launcher invocation mode is unsupported"
        )
    service_mode, manifest_schema, receipt_policy = binding
    candidate = _canonical_path_text(candidate_root)
    python_path = _canonical_path_text(python_executable)
    dependency_root = _canonical_path_text(python_dependency_root)
    launcher_source = _canonical_path_text(launcher_path)
    stage0_source = _canonical_path_text(stage0_path)
    service_source = _canonical_path_text(service_path)
    launcher = _canonical_path_text(launcher_staged_path or launcher_path)
    stage0 = _canonical_path_text(stage0_staged_path or stage0_path)
    service = _canonical_path_text(service_staged_path or service_path)
    root_texts = _canonical_root_texts(allowed_read_roots)
    receipt_path = (
        _canonical_path_text(no_order_receipt_output)
        if no_order_receipt_output is not None
        else None
    )
    if (receipt_policy == "required") != (receipt_path is not None):
        raise CapturedPaperActivationContractError(
            "LAUNCHER_RECEIPT_POLICY_INVALID",
            "launcher receipt output does not match the selected mode",
        )
    host_ready_path = (
        _canonical_path_text(host_ready_receipt)
        if host_ready_receipt is not None
        else None
    )
    if (mode == "ActivatePaper") != (host_ready_path is not None):
        raise CapturedPaperActivationContractError(
            "LAUNCHER_HOST_READY_POLICY_INVALID",
            "exactly ActivatePaper must bind a host-ready receipt base",
        )

    arguments: list[str] = [
        "-I",
        "-S",
        "-B",
        stage0,
        "--manifest",
        LAUNCHER_MANIFEST_PATH_TOKEN,
        "--manifest-sha256",
        LAUNCHER_MANIFEST_SHA256_TOKEN,
        "--candidate-root",
        candidate,
        "--target-role",
        "activation_service",
        "--target",
        service,
        "--target-sha256",
        _sha(service_sha256, "service_sha256"),
        "--",
        "--mode",
        service_mode,
        "--manifest",
        LAUNCHER_MANIFEST_PATH_TOKEN,
        "--manifest-sha256",
        LAUNCHER_MANIFEST_SHA256_TOKEN,
        "--candidate-root",
        candidate,
        "--launcher-path",
        launcher,
        "--launcher-sha256",
        _sha(launcher_sha256, "launcher_sha256"),
    ]
    for root in root_texts:
        arguments.extend(("--allow-read-root", root))
    if receipt_path is not None:
        arguments.extend(("--no-order-receipt-output", receipt_path))
    if host_ready_path is not None:
        arguments.extend(("--host-ready-receipt", host_ready_path))

    return {
        "allowed_read_roots": list(root_texts),
        "candidate_root": candidate,
        "foreground": True,
        "host_ready_receipt_base": host_ready_path,
        "launcher_source_path": launcher_source,
        "launcher_source_sha256": _sha(launcher_sha256, "launcher_sha256"),
        "launcher_path": launcher,
        "launcher_sha256": _sha(launcher_sha256, "launcher_sha256"),
        "manifest_path": LAUNCHER_MANIFEST_PATH_TOKEN,
        "manifest_schema_version": manifest_schema,
        "manifest_sha256": LAUNCHER_MANIFEST_SHA256_TOKEN,
        "mode": str(mode),
        "no_order_receipt_output_path": receipt_path,
        "no_order_receipt_output_policy": receipt_policy,
        "python_executable_path": python_path,
        "python_executable_sha256": _sha(
            python_executable_sha256, "python_executable_sha256"
        ),
        "python_dependency_root": dependency_root,
        "python_dependency_root_identity_sha256": _sha(
            python_dependency_root_identity_sha256,
            "python_dependency_root_identity_sha256",
        ),
        "python_import_root": candidate,
        "schema_version": LAUNCHER_INVOCATION_PROJECTION_SCHEMA_VERSION,
        "service_arguments": arguments,
        "service_mode": service_mode,
        "service_source_path": service_source,
        "service_source_sha256": _sha(service_sha256, "service_sha256"),
        "service_staged_path": service,
        "service_path": service,
        "service_sha256": _sha(service_sha256, "service_sha256"),
        "stage0_source_path": stage0_source,
        "stage0_source_sha256": _sha(stage0_sha256, "stage0_sha256"),
        "stage0_path": stage0,
        "stage0_sha256": _sha(stage0_sha256, "stage0_sha256"),
        "singleton_name": LAUNCHER_SINGLETON_NAME,
        "working_directory": candidate,
    }


def _stable_read(
    value: Any,
    *,
    expected_sha256: Any,
    roots: Sequence[Path],
    field: str,
    max_bytes: int = _MAX_ARTIFACT_BYTES,
    allow_empty: bool = False,
) -> tuple[Path, bytes, str]:
    expected = _sha(expected_sha256, f"{field}.sha256")
    path = Path(str(value or ""))
    if not _is_local_absolute(path):
        raise CapturedPaperActivationContractError(
            "NONLOCAL_PATH", f"{field} path must be absolute and local"
        )
    path = path.resolve(strict=True)
    if not _inside(path, roots):
        raise CapturedPaperActivationContractError(
            "PATH_OUTSIDE_ROOT", f"{field} path escaped the allowed roots"
        )
    _reject_reparse_chain(path)
    before = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or (
        before.st_size <= 0 and not allow_empty
    ):
        raise CapturedPaperActivationContractError(
            "INVALID_FILE", f"{field} is not a nonempty regular file"
        )
    if before.st_size > max_bytes:
        raise CapturedPaperActivationContractError(
            "FILE_TOO_LARGE", f"{field} exceeds the bounded read size"
        )
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    total = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise CapturedPaperActivationContractError(
                    "FILE_TOO_LARGE", f"{field} grew beyond the bounded read size"
                )
            digest.update(chunk)
            chunks.append(chunk)
    after = os.stat(path, follow_symlinks=False)
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after or total != after.st_size:
        raise CapturedPaperActivationContractError(
            "FILE_DRIFT", f"{field} changed while it was read"
        )
    actual = digest.hexdigest()
    if actual != expected:
        raise CapturedPaperActivationContractError(
            "HASH_MISMATCH", f"{field} content hash mismatch"
        )
    return path, b"".join(chunks), actual


def _strict_json(raw: bytes, field: str) -> Mapping[str, Any]:
    def pairs(rows: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in rows:
            if key in value:
                raise CapturedPaperActivationContractError(
                    "DUPLICATE_JSON_KEY", f"{field} repeats JSON key {key}"
                )
            value[key] = item
        return value

    def constant(value: str) -> Any:
        raise CapturedPaperActivationContractError(
            "NONFINITE_JSON", f"{field} contains non-finite JSON {value}"
        )

    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CapturedPaperActivationContractError(
            "INVALID_JSON", f"{field} is not strict UTF-8 JSON"
        ) from exc
    if not isinstance(decoded, Mapping):
        raise CapturedPaperActivationContractError(
            "INVALID_JSON", f"{field} root is not an object"
        )
    return decoded


def _validate_launcher_argument_contract(
    document: Mapping[str, Any],
    *,
    raw: bytes,
    candidate_root: Path,
    allowed_read_roots: Sequence[Path],
    source_paths: Mapping[str, Path],
    source_hashes: Mapping[str, str],
    activation_generation: str,
) -> Mapping[str, Mapping[str, Any]]:
    """Verify all three exact launcher projections against local source bytes."""

    _exact_keys(document, {"schema_version", "invocations"}, "launcher_arguments")
    if document.get("schema_version") != LAUNCHER_ARGUMENT_CONTRACT_SCHEMA_VERSION:
        raise CapturedPaperActivationContractError(
            "LAUNCHER_ARGUMENT_SCHEMA_MISMATCH",
            "launcher argument contract schema is unsupported",
        )
    if raw != _canonical_json_bytes(document):
        raise CapturedPaperActivationContractError(
            "LAUNCHER_ARGUMENTS_NOT_CANONICAL",
            "launcher argument contract is not canonical UTF-8 JSON",
        )
    invocations = _mapping(document.get("invocations"), "launcher_arguments.invocations")
    _exact_keys(invocations, _LAUNCHER_MODE_BINDINGS, "launcher_arguments.invocations")

    parsed: dict[str, Mapping[str, Any]] = {}
    for mode in sorted(_LAUNCHER_MODE_BINDINGS):
        entry = _mapping(invocations.get(mode), f"launcher_arguments.invocations.{mode}")
        _exact_keys(
            entry,
            {"projection", "projection_sha256"},
            f"launcher_arguments.invocations.{mode}",
        )
        projection = _mapping(
            entry.get("projection"),
            f"launcher_arguments.invocations.{mode}.projection",
        )
        if sha256_json(projection) != _sha(
            entry.get("projection_sha256"),
            f"launcher_arguments.invocations.{mode}.projection_sha256",
        ):
            raise CapturedPaperActivationContractError(
                "LAUNCHER_ARGUMENT_PROJECTION_HASH_MISMATCH",
                f"{mode} launcher projection hash mismatch",
            )
        parsed[mode] = projection

    seed = parsed["ValidateOnly"]
    python_path, _python_raw, python_sha = _stable_read(
        seed.get("python_executable_path"),
        expected_sha256=seed.get("python_executable_sha256"),
        roots=allowed_read_roots,
        field="launcher_arguments.python_executable",
    )
    dependency_root = Path(str(seed.get("python_dependency_root") or ""))
    if not _is_local_absolute(dependency_root):
        raise CapturedPaperActivationContractError(
            "PYTHON_DEPENDENCY_ROOT_INVALID",
            "launcher dependency root must be absolute and local",
        )
    dependency_root = dependency_root.resolve(strict=True)
    _reject_reparse_chain(dependency_root)
    _reject_network_drive(dependency_root)
    if not dependency_root.is_dir() or not _inside(dependency_root, allowed_read_roots):
        raise CapturedPaperActivationContractError(
            "PYTHON_DEPENDENCY_ROOT_INVALID",
            "launcher dependency root escaped allowed roots",
        )
    dependency_identity_sha = python_dependency_root_identity_sha256(
        dependency_root=dependency_root,
        python_executable=python_path,
        python_executable_sha256=python_sha,
    )
    if dependency_identity_sha != _sha(
        seed.get("python_dependency_root_identity_sha256"),
        "launcher_arguments.python_dependency_root_identity_sha256",
    ):
        raise CapturedPaperActivationContractError(
            "PYTHON_DEPENDENCY_ROOT_IDENTITY_MISMATCH",
            "launcher dependency root identity differs",
        )
    receipt_path = _strict_local_output_path(
        parsed["NoOrderSmoke"].get("no_order_receipt_output_path"),
        roots=allowed_read_roots,
        field="launcher_arguments.no_order_receipt_output_path",
    )
    launcher_path = source_paths["activation_launcher"]
    stage0_path = source_paths["activation_stage0"]
    service_path = source_paths["activation_service"]
    activate = parsed["ActivatePaper"]
    staged_launcher, staged_launcher_raw, staged_launcher_sha = _stable_read(
        activate.get("launcher_path"),
        expected_sha256=activate.get("launcher_sha256"),
        roots=allowed_read_roots,
        field="launcher_arguments.staged_launcher",
    )
    staged_service, staged_service_raw, staged_service_sha = _stable_read(
        activate.get("service_staged_path"),
        expected_sha256=activate.get("service_sha256"),
        roots=allowed_read_roots,
        field="launcher_arguments.staged_service",
    )
    staged_stage0, staged_stage0_raw, staged_stage0_sha = _stable_read(
        activate.get("stage0_path"),
        expected_sha256=activate.get("stage0_sha256"),
        roots=allowed_read_roots,
        field="launcher_arguments.staged_stage0",
    )
    launcher_source_path, launcher_source_raw, launcher_source_sha = _stable_read(
        activate.get("launcher_source_path"),
        expected_sha256=activate.get("launcher_source_sha256"),
        roots=(candidate_root,),
        field="launcher_arguments.launcher_source",
    )
    service_source_path, service_source_raw, service_source_sha = _stable_read(
        activate.get("service_source_path"),
        expected_sha256=activate.get("service_source_sha256"),
        roots=(candidate_root,),
        field="launcher_arguments.service_source",
    )
    stage0_source_path, stage0_source_raw, stage0_source_sha = _stable_read(
        activate.get("stage0_source_path"),
        expected_sha256=activate.get("stage0_source_sha256"),
        roots=(candidate_root,),
        field="launcher_arguments.stage0_source",
    )
    generation = _uuid(activation_generation, "activation_generation")
    if not (
        launcher_source_path == launcher_path
        and launcher_source_sha == source_hashes["activation_launcher"]
        and service_source_path == service_path
        and service_source_sha == source_hashes["activation_service"]
        and stage0_source_path == stage0_path
        and stage0_source_sha == source_hashes["activation_stage0"]
        and staged_launcher_raw == launcher_source_raw
        and staged_service_raw == service_source_raw
        and staged_stage0_raw == stage0_source_raw
        and staged_launcher_sha == launcher_source_sha
        and staged_service_sha == service_source_sha
        and staged_stage0_sha == stage0_source_sha
        and staged_launcher.name.casefold() == f"{launcher_source_sha}.ps1"
        and staged_service.name.casefold() == f"{service_source_sha}.py"
        and staged_stage0.name.casefold() == f"{stage0_source_sha}.py"
        and staged_launcher.parent.name.casefold() == launcher_source_sha
        and staged_service.parent.name.casefold() == service_source_sha
        and staged_stage0.parent.name.casefold() == stage0_source_sha
        and staged_launcher.parent.parent.name.casefold() == generation
        and staged_service.parent.parent.name.casefold() == generation
        and staged_stage0.parent.parent.name.casefold() == generation
        and staged_launcher.parent.parent == staged_service.parent.parent
        and staged_launcher.parent.parent == staged_stage0.parent.parent
    ):
        raise CapturedPaperActivationContractError(
            "STAGED_ENTRYPOINT_BINDING_MISMATCH",
            "staged launcher/service are not exact generation-owned source copies",
        )
    host_ready = _strict_local_output_path(
        activate.get("host_ready_receipt_base"),
        roots=allowed_read_roots,
        field="launcher_arguments.host_ready_receipt_base",
    )
    if (
        host_ready.parent.name.casefold() != "handshake"
        or host_ready.parent.parent.name.casefold() != generation
        or host_ready.parent.parent != staged_launcher.parent.parent
    ):
        raise CapturedPaperActivationContractError(
            "HOST_READY_RECEIPT_INVALID",
            "host-ready base is not generation-owned and append-only new",
        )
    for mode in sorted(_LAUNCHER_MODE_BINDINGS):
        expected = launcher_invocation_projection(
            mode=mode,
            candidate_root=candidate_root,
            python_executable=python_path,
            python_executable_sha256=python_sha,
            python_dependency_root=dependency_root,
            python_dependency_root_identity_sha256=dependency_identity_sha,
            allowed_read_roots=allowed_read_roots,
            launcher_path=launcher_path,
            launcher_sha256=source_hashes["activation_launcher"],
            stage0_path=stage0_path,
            stage0_sha256=source_hashes["activation_stage0"],
            service_path=service_path,
            service_sha256=source_hashes["activation_service"],
            launcher_staged_path=staged_launcher,
            stage0_staged_path=staged_stage0,
            service_staged_path=staged_service,
            host_ready_receipt=(host_ready if mode == "ActivatePaper" else None),
            no_order_receipt_output=(
                receipt_path if mode == "NoOrderSmoke" else None
            ),
        )
        if _canonical_json_bytes(parsed[mode]) != _canonical_json_bytes(expected):
            raise CapturedPaperActivationContractError(
                "LAUNCHER_ARGUMENT_BINDING_MISMATCH",
                f"{mode} launcher projection differs from the verified invocation",
            )
    return MappingProxyType(parsed)


def _self_digest(value: Mapping[str, Any], field: str) -> str:
    claimed = _sha(value.get(f"{field}_sha256"), f"{field}_sha256")
    body = dict(value)
    body.pop(f"{field}_sha256", None)
    if sha256_json(body) != claimed:
        raise CapturedPaperActivationContractError(
            "SELF_DIGEST_MISMATCH", f"{field} self digest mismatch"
        )
    return claimed


def _validate_no_order_phase_one_evidence(
    value: Any,
    *,
    activation_generation: str,
) -> str:
    receipt = _mapping(value, "no_order_smoke.phase_one_reconciliation")
    expected_keys = {
        "schema_version",
        "activation_generation",
        "initial_pending_count",
        "remaining_pending_count",
        "reconciliation_complete",
        "outbox_committed_count",
        "decision_handoff_unavailable_count",
        "outbox_committed_completion_sha256s",
        "decision_handoff_unavailable_completion_sha256s",
        "phase_two_side_effects_inferred",
        "receipt_sha256",
    }
    _exact_keys(receipt, expected_keys, "no_order_smoke.phase_one_reconciliation")
    initial = receipt.get("initial_pending_count")
    committed = receipt.get("outbox_committed_count")
    unavailable = receipt.get("decision_handoff_unavailable_count")
    committed_ids = receipt.get("outbox_committed_completion_sha256s")
    unavailable_ids = receipt.get(
        "decision_handoff_unavailable_completion_sha256s"
    )
    if not (
        receipt.get("schema_version")
        == "chili.captured-paper-phase-one-restart-reconciliation.v1"
        and receipt.get("activation_generation") == activation_generation
        and receipt.get("remaining_pending_count") == 0
        and receipt.get("reconciliation_complete") is True
        and receipt.get("phase_two_side_effects_inferred") is False
        and all(
            isinstance(item, int) and not isinstance(item, bool) and item >= 0
            for item in (initial, committed, unavailable)
        )
        and initial == committed + unavailable
        and isinstance(committed_ids, list)
        and isinstance(unavailable_ids, list)
        and committed_ids == sorted(set(committed_ids))
        and unavailable_ids == sorted(set(unavailable_ids))
        and len(committed_ids) == committed
        and len(unavailable_ids) == unavailable
        and not set(committed_ids).intersection(unavailable_ids)
    ):
        raise CapturedPaperActivationContractError(
            "PHASE_ONE_RECONCILIATION_INVALID",
            "no-order phase-one reconciliation is not exhaustive",
        )
    for index, digest in enumerate(committed_ids):
        _sha(digest, f"phase_one.outbox_committed[{index}]")
    for index, digest in enumerate(unavailable_ids):
        _sha(digest, f"phase_one.handoff_unavailable[{index}]")
    claimed = _sha(receipt.get("receipt_sha256"), "phase_one.receipt_sha256")
    body = dict(receipt)
    body.pop("receipt_sha256")
    if sha256_json(body) != claimed:
        raise CapturedPaperActivationContractError(
            "PHASE_ONE_RECONCILIATION_INVALID",
            "no-order phase-one reconciliation digest is invalid",
        )
    return claimed


def _validate_no_order_restart_gate_evidence(
    value: Any,
    *,
    activation_generation: str,
    expected_account_id: str,
    code_build_sha256: str,
    effective_config_sha256: str,
    capture_receipt_sha256: str,
    preactivation_manifest_sha256: str,
    phase_one_receipt_sha256: str,
) -> datetime:
    receipt = _mapping(value, "no_order_smoke.restart_inventory_gate")
    body_keys = {
        "schema_version",
        "account_scope",
        "expected_account_id",
        "runtime_generation",
        "broker_connection_generation",
        "broker_adapter_build_sha256",
        "broker_read_binding_canonical_json",
        "broker_read_binding_sha256",
        "phase_one_reconciliation_receipt_sha256",
        "opening_open_order_census_sha256",
        "opening_position_census_sha256",
        "closing_position_census_sha256",
        "closing_open_order_census_sha256",
        "opening_restart_receipt_sha256",
        "closing_restart_receipt_sha256",
        "stable_inventory_projection_sha256",
        "durable_inventory_sha256",
        "open_order_inventory_sha256",
        "position_inventory_sha256",
        "disposition",
        "recovery_required",
        "new_admissions_quarantined",
        "exposure_decreasing_only",
        "broker_inventory_flat",
        "observed_at",
        "paper_execution_only",
        "live_cash_authorized",
        "real_money_authorized",
    }
    _exact_keys(
        receipt,
        body_keys | {"receipt_canonical_json", "receipt_sha256"},
        "no_order_smoke.restart_inventory_gate",
    )
    canonical = receipt.get("receipt_canonical_json")
    if not isinstance(canonical, str):
        raise CapturedPaperActivationContractError(
            "RESTART_GATE_INVALID", "restart gate canonical body is missing"
        )
    try:
        decoded = json.loads(canonical)
        binding = json.loads(
            str(receipt.get("broker_read_binding_canonical_json") or "")
        )
    except (json.JSONDecodeError, TypeError) as exc:
        raise CapturedPaperActivationContractError(
            "RESTART_GATE_INVALID", "restart gate evidence is not canonical JSON"
        ) from exc
    echoed = dict(receipt)
    echoed.pop("receipt_canonical_json")
    echoed.pop("receipt_sha256")
    claimed = _sha(receipt.get("receipt_sha256"), "restart_gate.receipt_sha256")
    if not (
        isinstance(decoded, dict)
        and set(decoded) == body_keys
        and decoded == echoed
        and _canonical_json_bytes(decoded).decode("utf-8") == canonical
        and hashlib.sha256(canonical.encode("utf-8")).hexdigest() == claimed
    ):
        raise CapturedPaperActivationContractError(
            "RESTART_GATE_INVALID", "restart gate body or digest is inconsistent"
        )
    binding_keys = {
        "schema_version",
        "purpose",
        "activation_generation",
        "activation_manifest_sha256",
        "code_build_sha256",
        "settings_projection_sha256",
        "capture_receipt_sha256",
        "expected_account_id",
        "connection_receipt_sha256",
        "adapter_connection_generation",
        "adapter_build_sha256",
        "phase_one_reconciliation_receipt_sha256",
    }
    if not isinstance(binding, dict):
        raise CapturedPaperActivationContractError(
            "RESTART_GATE_INVALID", "restart gate read binding is missing"
        )
    _exact_keys(binding, binding_keys, "restart_gate.read_binding")
    connection_generation = str(
        receipt.get("broker_connection_generation") or ""
    )
    if not connection_generation.startswith("alpaca-paper-rest:"):
        raise CapturedPaperActivationContractError(
            "RESTART_GATE_INVALID", "restart gate connection is not Alpaca PAPER"
        )
    _sha(connection_generation.split(":", 1)[1], "restart_gate.connection_generation")
    adapter_build_sha256 = _sha(
        receipt.get("broker_adapter_build_sha256"),
        "restart_gate.adapter_build_sha256",
    )
    binding_json = str(receipt.get("broker_read_binding_canonical_json") or "")
    empty_inventory_sha256 = hashlib.sha256(b"[]").hexdigest()
    if not (
        _canonical_json_bytes(binding).decode("utf-8") == binding_json
        and hashlib.sha256(binding_json.encode("utf-8")).hexdigest()
        == _sha(
            receipt.get("broker_read_binding_sha256"),
            "restart_gate.read_binding_sha256",
        )
        and binding
        == {
            "schema_version": "chili.captured-paper-restart-read-binding.v1",
            "purpose": "captured_paper_restart_inventory",
            "activation_generation": activation_generation,
            "activation_manifest_sha256": preactivation_manifest_sha256,
            "code_build_sha256": code_build_sha256,
            "settings_projection_sha256": effective_config_sha256,
            "capture_receipt_sha256": capture_receipt_sha256,
            "expected_account_id": expected_account_id,
            "connection_receipt_sha256": _sha(
                binding.get("connection_receipt_sha256"),
                "restart_gate.connection_receipt_sha256",
            ),
            "adapter_connection_generation": connection_generation,
            "adapter_build_sha256": adapter_build_sha256,
            "phase_one_reconciliation_receipt_sha256": phase_one_receipt_sha256,
        }
        and receipt.get("schema_version")
        == "chili.captured-paper-restart-gate.v1"
        and receipt.get("account_scope") == "alpaca:paper"
        and receipt.get("expected_account_id") == expected_account_id
        and receipt.get("runtime_generation") == activation_generation
        and receipt.get("phase_one_reconciliation_receipt_sha256")
        == phase_one_receipt_sha256
        and receipt.get("disposition") == "strict_flat_first_cutover"
        and receipt.get("recovery_required") is False
        and receipt.get("new_admissions_quarantined") is False
        and receipt.get("exposure_decreasing_only") is False
        and receipt.get("broker_inventory_flat") is True
        and receipt.get("paper_execution_only") is True
        and receipt.get("live_cash_authorized") is False
        and receipt.get("real_money_authorized") is False
        and receipt.get("opening_open_order_census_sha256")
        != receipt.get("closing_open_order_census_sha256")
        and receipt.get("opening_position_census_sha256")
        != receipt.get("closing_position_census_sha256")
        and receipt.get("opening_restart_receipt_sha256")
        != receipt.get("closing_restart_receipt_sha256")
        and receipt.get("durable_inventory_sha256")
        == empty_inventory_sha256
        and receipt.get("open_order_inventory_sha256")
        == empty_inventory_sha256
        and receipt.get("position_inventory_sha256")
        == empty_inventory_sha256
    ):
        raise CapturedPaperActivationContractError(
            "RESTART_GATE_INVALID",
            "restart gate is not strict-flat or activation-bound",
        )
    digest_fields = body_keys - {
        "schema_version",
        "account_scope",
        "expected_account_id",
        "runtime_generation",
        "broker_connection_generation",
        "broker_read_binding_canonical_json",
        "disposition",
        "recovery_required",
        "new_admissions_quarantined",
        "exposure_decreasing_only",
        "broker_inventory_flat",
        "observed_at",
        "paper_execution_only",
        "live_cash_authorized",
        "real_money_authorized",
    }
    for field in digest_fields:
        _sha(receipt.get(field), f"restart_gate.{field}")
    return _parse_utc(receipt.get("observed_at"), "restart_gate.observed_at")


def _validate_receipt(
    *,
    kind: str,
    document: Mapping[str, Any],
    now: datetime,
    activation_generation: str,
    expected_account_id: str,
    code_build_sha256: str,
    effective_config_sha256: str,
    capture_receipt_sha256: str,
    preactivation_manifest_sha256: str | None = None,
    runtime_environment_sha256: str | None = None,
    database_target_fingerprint: str | None = None,
    iqfeed_bootstrap_manifest_sha256: str | None = None,
    launcher_argument_contract_sha256: str | None = None,
    capture_store_root: str | None = None,
    source_hashes: Mapping[str, str] | None = None,
    allowed_read_roots: Sequence[Path] = (),
    require_artifact_probe: bool = True,
) -> None:
    try:
        context = readiness_evidence.ReadinessValidationContext(
            activation_generation=activation_generation,
            expected_account_id=expected_account_id,
            code_build_sha256=code_build_sha256,
            effective_config_sha256=effective_config_sha256,
            capture_receipt_sha256=capture_receipt_sha256,
            runtime_environment_sha256=_sha(
                runtime_environment_sha256,
                "runtime_environment_sha256",
            ),
            database_target_fingerprint=_sha(
                database_target_fingerprint,
                "database_target_fingerprint",
            ),
            iqfeed_bootstrap_manifest_sha256=_sha(
                iqfeed_bootstrap_manifest_sha256,
                "iqfeed_bootstrap_manifest_sha256",
            ),
            launcher_argument_contract_sha256=_sha(
                launcher_argument_contract_sha256,
                "launcher_argument_contract_sha256",
            ),
            capture_store_root=str(capture_store_root or ""),
            source_hashes=MappingProxyType(dict(source_hashes or {})),
            allowed_read_roots=tuple(str(path) for path in allowed_read_roots),
        )
        if kind != "no_order_smoke":
            if require_artifact_probe:
                readiness_evidence.validate_readiness_receipt_v3(
                    document,
                    kind=kind,
                    context=context,
                    now=now,
                    max_age_seconds=_RECEIPT_MAX_AGE_SECONDS[kind],
                )
            else:
                readiness_evidence.validate_readiness_receipt_v2(
                    document,
                    kind=kind,
                    context=context,
                    now=now,
                    max_age_seconds=_RECEIPT_MAX_AGE_SECONDS[kind],
                )
            return
    except readiness_evidence.CapturedPaperReadinessEvidenceError as exc:
        raise CapturedPaperActivationContractError(
            "TYPED_READINESS_EVIDENCE_INVALID",
            f"{kind} typed readiness evidence is invalid: {exc}",
        ) from exc

    expected_keys = {
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
        "live_cash_authorized",
        "orders_submitted",
        "checks",
        "receipt_sha256",
    }
    restart_observed_at: datetime | None = None
    if kind == "no_order_smoke":
        expected_keys.add("preactivation_manifest_sha256")
        expected_keys.add("order_submission_audit")
        expected_keys.add("phase_one_reconciliation")
        expected_keys.add("restart_inventory_gate")
        expected_keys.add("refreshed_readiness")
    _exact_keys(
        document,
        expected_keys,
        f"readiness_receipts.{kind}",
    )
    expected_receipt_schema = f"{RECEIPT_SCHEMA_PREFIX}{kind}.v4"
    if (
        document.get("schema_version") != expected_receipt_schema
        or document.get("receipt_kind") != kind
        or document.get("verdict") != "PASS"
    ):
        raise CapturedPaperActivationContractError(
            "RECEIPT_VERDICT_INVALID", f"{kind} readiness receipt did not pass"
        )
    exact = {
        "activation_generation": activation_generation,
        "account_scope": "alpaca:paper",
        "expected_account_id": expected_account_id,
        "code_build_sha256": code_build_sha256,
        "effective_config_sha256": effective_config_sha256,
        "capture_receipt_sha256": capture_receipt_sha256,
        "live_cash_authorized": False,
        "orders_submitted": False,
    }
    exact["preactivation_manifest_sha256"] = _sha(
        preactivation_manifest_sha256,
        "preactivation_manifest_sha256",
    )
    if any(document.get(name) != value for name, value in exact.items()):
        raise CapturedPaperActivationContractError(
            "RECEIPT_BINDING_MISMATCH", f"{kind} readiness binding mismatch"
        )
    if kind == "no_order_smoke":
        audit = _mapping(
            document.get("order_submission_audit"),
            "no_order_smoke.order_submission_audit",
        )
        _exact_keys(
            audit,
            {
                "audit_generation",
                "before_call_count",
                "after_call_count",
                "call_count_delta",
                "before_chain_sha256",
                "after_chain_sha256",
                "before_snapshot_sha256",
                "after_snapshot_sha256",
            },
            "no_order_smoke.order_submission_audit",
        )
        _uuid(
            audit.get("audit_generation"),
            "no_order_smoke.order_submission_audit.audit_generation",
        )
        before_count = audit.get("before_call_count")
        after_count = audit.get("after_call_count")
        delta = audit.get("call_count_delta")
        if not (
            isinstance(before_count, int)
            and not isinstance(before_count, bool)
            and before_count >= 0
            and isinstance(after_count, int)
            and not isinstance(after_count, bool)
            and after_count >= 0
            and isinstance(delta, int)
            and not isinstance(delta, bool)
            and delta == after_count - before_count == 0
            and _sha(
                audit.get("before_chain_sha256"),
                "no_order_smoke.before_chain_sha256",
            )
            == _sha(
                audit.get("after_chain_sha256"),
                "no_order_smoke.after_chain_sha256",
            )
            and _sha(
                audit.get("before_snapshot_sha256"),
                "no_order_smoke.before_snapshot_sha256",
            )
            == _sha(
                audit.get("after_snapshot_sha256"),
                "no_order_smoke.after_snapshot_sha256",
            )
        ):
            raise CapturedPaperActivationContractError(
                "NO_ORDER_SUBMISSION_AUDIT_INVALID",
                "no-order smoke did not prove an unchanged exact adapter submission census",
            )
        phase_one_sha256 = _validate_no_order_phase_one_evidence(
            document.get("phase_one_reconciliation"),
            activation_generation=activation_generation,
        )
        restart_observed_at = _validate_no_order_restart_gate_evidence(
            document.get("restart_inventory_gate"),
            activation_generation=activation_generation,
            expected_account_id=expected_account_id,
            code_build_sha256=code_build_sha256,
            effective_config_sha256=effective_config_sha256,
            capture_receipt_sha256=capture_receipt_sha256,
            preactivation_manifest_sha256=_sha(
                preactivation_manifest_sha256,
                "preactivation_manifest_sha256",
            ),
            phase_one_receipt_sha256=phase_one_sha256,
        )
    captured_at = _parse_utc(document.get("captured_at"), f"{kind}.captured_at")
    expires_at = _parse_utc(document.get("expires_at"), f"{kind}.expires_at")
    if captured_at > now or expires_at < now or expires_at <= captured_at:
        raise CapturedPaperActivationContractError(
            "RECEIPT_STALE", f"{kind} readiness receipt is stale or future-dated"
        )
    if restart_observed_at is not None and not (
        timedelta(0) <= captured_at - restart_observed_at <= timedelta(seconds=60)
    ):
        raise CapturedPaperActivationContractError(
            "RESTART_GATE_STALE",
            "no-order restart gate is stale or future-dated",
        )
    refreshed_expiries: list[datetime] = []
    if kind == "no_order_smoke":
        refreshed = _mapping(
            document.get("refreshed_readiness"),
            "no_order_smoke.refreshed_readiness",
        )
        _exact_keys(
            refreshed,
            {"broker_account", "kill_switch"},
            "no_order_smoke.refreshed_readiness",
        )
        for refreshed_kind in ("broker_account", "kill_switch"):
            refreshed_document = _mapping(
                refreshed.get(refreshed_kind),
                f"no_order_smoke.refreshed_readiness.{refreshed_kind}",
            )
            try:
                refreshed_captured, refreshed_expires = (
                    readiness_evidence.validate_readiness_receipt_v2(
                        refreshed_document,
                        kind=refreshed_kind,
                        context=context,
                        now=now,
                        # 2026-07-17: bound to the single authority table so
                        # the nested refreshed receipts share the post-smoke
                        # class the service now issues them with (a hardcoded
                        # 30 rejected any receipt whose expires-captured
                        # window exceeded 30s).
                        max_age_seconds=_RECEIPT_MAX_AGE_SECONDS[
                            refreshed_kind
                        ],
                    )
                )
            except readiness_evidence.CapturedPaperReadinessEvidenceError as exc:
                raise CapturedPaperActivationContractError(
                    "POST_SMOKE_READINESS_INVALID",
                    f"no-order {refreshed_kind} refresh is invalid: {exc}",
                ) from exc
            if not (
                restart_observed_at is not None
                and restart_observed_at <= refreshed_captured <= captured_at
                and (captured_at - refreshed_captured).total_seconds() <= 10.0
            ):
                raise CapturedPaperActivationContractError(
                    "POST_SMOKE_READINESS_STALE",
                    f"no-order {refreshed_kind} refresh is stale or misordered",
                )
            refreshed_expiries.append(refreshed_expires)
        if any(expires_at > value for value in refreshed_expiries):
            raise CapturedPaperActivationContractError(
                "POST_SMOKE_READINESS_STALE",
                "no-order authority outlives a nested refreshed receipt",
            )
    age = (now - captured_at).total_seconds()
    if age > _RECEIPT_MAX_AGE_SECONDS[kind]:
        raise CapturedPaperActivationContractError(
            "RECEIPT_STALE", f"{kind} readiness receipt exceeded its fixed maximum age"
        )
    checks = _mapping(document.get("checks"), f"{kind}.checks")
    _exact_keys(checks, _REQUIRED_CHECKS[kind], f"{kind}.checks")
    if any(value is not True for value in checks.values()):
        raise CapturedPaperActivationContractError(
            "RECEIPT_CHECK_FAILED", f"{kind} readiness receipt contains a failed check"
        )
    _self_digest(document, "receipt")


def _load_captured_paper_envelope(
    manifest_path: str | Path,
    *,
    expected_manifest_sha256: str,
    candidate_root: str | Path,
    allowed_read_roots: Sequence[str | Path],
    envelope_stage: str,
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    _superseded_readiness_kinds: frozenset[str] = frozenset(),
) -> VerifiedCapturedPaperActivation:
    """Verify one exact stage of the two-stage PAPER envelope locally."""

    if envelope_stage not in {"preactivation", "activation"}:
        raise CapturedPaperActivationContractError(
            "ENVELOPE_STAGE_INVALID", "PAPER envelope stage is unsupported"
        )
    is_activation = envelope_stage == "activation"
    if not _superseded_readiness_kinds.issubset(
        {"broker_account", "kill_switch"}
    ):
        raise CapturedPaperActivationContractError(
            "READINESS_SUPERSESSION_INVALID",
            "only short-lived broker/kill readiness may be superseded",
        )

    roots = _roots(allowed_read_roots)
    root = Path(candidate_root)
    if not _is_local_absolute(root):
        raise CapturedPaperActivationContractError(
            "INVALID_CANDIDATE_ROOT", "candidate root must be absolute and local"
        )
    root = root.resolve(strict=True)
    _reject_reparse_chain(root)
    if not root.is_dir() or not _inside(root, roots):
        raise CapturedPaperActivationContractError(
            "INVALID_CANDIDATE_ROOT", "candidate root escaped the allowed roots"
        )
    now = wall_clock()
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise CapturedPaperActivationContractError(
            "INVALID_CLOCK", "activation wall clock is not timezone-aware"
        )
    now = now.astimezone(UTC)
    manifest_file, raw, manifest_sha = _stable_read(
        manifest_path,
        expected_sha256=expected_manifest_sha256,
        roots=roots,
        field="activation_manifest",
        max_bytes=_MAX_MANIFEST_BYTES,
    )
    document = _strict_json(raw, "activation_manifest")
    document_keys = {
            "schema_version",
            "generated_at",
            "expires_at",
            "activation_generation",
            "authority_boundary",
            "runtime_environment",
            "code_build",
            "capture_binding",
            "iqfeed_bootstrap",
            "readiness_receipts",
            "cutover",
            "capture_store_root",
            "activation_manifest_sha256",
        }
    if is_activation:
        document_keys.add("preactivation_binding")
    expected_schema = (
        ACTIVATION_MANIFEST_SCHEMA_VERSION
        if is_activation
        else PREACTIVATION_MANIFEST_SCHEMA_VERSION
    )
    if document.get("schema_version") != expected_schema:
        raise CapturedPaperActivationContractError(
            "MANIFEST_SCHEMA_MISMATCH",
            f"{envelope_stage} manifest schema is unsupported",
        )
    _exact_keys(document, document_keys, "activation_manifest")
    _self_digest(document, "activation_manifest")
    generated_at = _parse_utc(document.get("generated_at"), "generated_at")
    expires_at = _parse_utc(document.get("expires_at"), "expires_at")
    age = (now - generated_at).total_seconds()
    if (
        age < -_MAX_FUTURE_SKEW_SECONDS
        or age > _MAX_MANIFEST_AGE_SECONDS
        or expires_at < now
        or expires_at <= generated_at
        or (expires_at - generated_at).total_seconds() > _MAX_MANIFEST_AGE_SECONDS
    ):
        raise CapturedPaperActivationContractError(
            "MANIFEST_STALE", "activation manifest is stale, future-dated, or overlong"
        )
    generation = _uuid(document.get("activation_generation"), "activation_generation")

    boundary = _mapping(document.get("authority_boundary"), "authority_boundary")
    _exact_keys(
        boundary,
        {
            "broker",
            "broker_environment",
            "account_scope",
            "expected_account_id",
            "equity_long_only",
            "first_dip_policy_mode",
            "paper_order_submission_authorized",
            "live_cash_authorized",
            "short_authorized",
            "crypto_authorized",
            "real_money_authorized",
        },
        "authority_boundary",
    )
    expected_account_id = _uuid(
        boundary.get("expected_account_id"), "expected_account_id"
    )
    boundary_exact = {
        "broker": "alpaca",
        "broker_environment": "paper",
        "account_scope": "alpaca:paper",
        "equity_long_only": True,
        "first_dip_policy_mode": "candidate",
        "paper_order_submission_authorized": is_activation,
        "live_cash_authorized": False,
        "short_authorized": False,
        "crypto_authorized": False,
        "real_money_authorized": False,
    }
    if any(boundary.get(name) != value for name, value in boundary_exact.items()):
        raise CapturedPaperActivationContractError(
            "AUTHORITY_BOUNDARY_OPEN",
            f"{envelope_stage} authority escaped Alpaca PAPER boundary",
        )

    runtime = _mapping(document.get("runtime_environment"), "runtime_environment")
    _exact_keys(
        runtime,
        {
            "source_env_path",
            "source_env_sha256",
            "runtime_environment_sha256",
            "effective_config_sha256",
            "database_target_fingerprint",
        },
        "runtime_environment",
    )
    _stable_read(
        runtime.get("source_env_path"),
        expected_sha256=runtime.get("source_env_sha256"),
        roots=roots,
        field="runtime_environment.source_env",
    )
    _sha(runtime.get("runtime_environment_sha256"), "runtime_environment_sha256")
    effective_config_sha256 = _sha(
        runtime.get("effective_config_sha256"), "effective_config_sha256"
    )
    _sha(runtime.get("database_target_fingerprint"), "database_target_fingerprint")

    code_build = _mapping(document.get("code_build"), "code_build")
    _exact_keys(
        code_build,
        {"schema_version", "artifacts", "code_build_sha256"},
        "code_build",
    )
    if code_build.get("schema_version") != CODE_BUILD_SCHEMA_VERSION:
        raise CapturedPaperActivationContractError(
            "CODE_BUILD_SCHEMA_MISMATCH", "code-build schema is unsupported"
        )
    rows = code_build.get("artifacts")
    if not isinstance(rows, list):
        raise CapturedPaperActivationContractError(
            "CODE_BUILD_INVALID", "code-build artifacts are not an array"
        )
    source_paths: dict[str, Path] = {}
    source_hashes: dict[str, str] = {}
    path_roles: dict[Path, str] = {}
    normalized_rows: list[dict[str, str]] = []
    for row in rows:
        item = _mapping(row, "code_build.artifact")
        _exact_keys(item, {"role", "path", "sha256"}, "code_build.artifact")
        role = str(item.get("role") or "").strip().lower()
        if not role or role in source_paths:
            raise CapturedPaperActivationContractError(
                "CODE_ROLE_INVALID", "code-build role is empty or duplicated"
            )
        path, _bytes, digest = _stable_read(
            item.get("path"),
            expected_sha256=item.get("sha256"),
            roots=(root,),
            field=f"code_build.{role}",
            allow_empty=role.startswith(_DEPENDENCY_ROLE_PREFIX),
        )
        if path in path_roles:
            raise CapturedPaperActivationContractError(
                "CODE_ROLE_INVALID",
                f"code-build path is duplicated by {path_roles[path]} and {role}",
            )
        path_roles[path] = role
        source_paths[role] = path
        source_hashes[role] = digest
        normalized_rows.append({"role": role, "path": str(path), "sha256": digest})
    missing_primary = _REQUIRED_CODE_ROLES - set(source_paths)
    if missing_primary:
        raise CapturedPaperActivationContractError(
            "CODE_ROSTER_MISMATCH",
            f"code-build primary roles differ; missing={sorted(missing_primary)}",
        )
    primary_paths = {source_paths[role] for role in _REQUIRED_CODE_ROLES}
    closure = discover_captured_paper_local_dependency_closure(
        candidate_root=root,
        seed_paths=primary_paths,
    )
    expected_dependencies = {
        dependency_role(module_name): path
        for module_name, path in closure.items()
        if path not in primary_paths
    }
    expected_roles = set(_REQUIRED_CODE_ROLES) | set(expected_dependencies)
    if set(source_paths) != expected_roles:
        raise CapturedPaperActivationContractError(
            "CODE_DEPENDENCY_CLOSURE_MISMATCH",
            "code-build does not exactly bind the local PAPER dependency closure; "
            f"missing={sorted(expected_roles-set(source_paths))} "
            f"extra={sorted(set(source_paths)-expected_roles)}",
        )
    for role, expected_path in expected_dependencies.items():
        if source_paths[role] != expected_path:
            raise CapturedPaperActivationContractError(
                "CODE_DEPENDENCY_CLOSURE_MISMATCH",
                f"local dependency role {role} points to another source",
            )
    if normalized_rows != sorted(normalized_rows, key=lambda row: row["role"]):
        raise CapturedPaperActivationContractError(
            "CODE_ROSTER_UNSORTED", "code-build roles must be sorted"
        )
    code_body = {
        "schema_version": CODE_BUILD_SCHEMA_VERSION,
        "artifacts": normalized_rows,
    }
    code_build_sha256 = _sha(code_build.get("code_build_sha256"), "code_build_sha256")
    if sha256_json(code_body) != code_build_sha256:
        raise CapturedPaperActivationContractError(
            "CODE_BUILD_HASH_MISMATCH", "code-build digest did not bind the source roster"
        )

    capture_ref = _mapping(document.get("capture_binding"), "capture_binding")
    _exact_keys(capture_ref, {"path", "sha256"}, "capture_binding")
    _capture_path, capture_raw, capture_receipt_sha256 = _stable_read(
        capture_ref.get("path"),
        expected_sha256=capture_ref.get("sha256"),
        roots=roots,
        field="capture_binding",
    )
    capture_doc = _strict_json(capture_raw, "capture_binding")
    _exact_keys(
        capture_doc,
        {
            "schema_version",
            "verdict",
            "activation_generation",
            "account_scope",
            "expected_account_id",
            "code_build_sha256",
            "effective_config_sha256",
            "live_cash_authorized",
            "network_fallback_allowed",
            "current_database_fallback_allowed",
        },
        "capture_binding",
    )
    capture_exact = {
        "schema_version": CAPTURE_BINDING_SCHEMA_VERSION,
        "verdict": "PASS",
        "activation_generation": generation,
        "account_scope": "alpaca:paper",
        "expected_account_id": expected_account_id,
        "code_build_sha256": code_build_sha256,
        "effective_config_sha256": effective_config_sha256,
        "live_cash_authorized": False,
        "network_fallback_allowed": False,
        "current_database_fallback_allowed": False,
    }
    if dict(capture_doc) != capture_exact:
        raise CapturedPaperActivationContractError(
            "CAPTURE_BINDING_MISMATCH", "capture binding escaped the activation generation"
        )

    bootstrap_ref = _mapping(document.get("iqfeed_bootstrap"), "iqfeed_bootstrap")
    _exact_keys(bootstrap_ref, {"path", "sha256"}, "iqfeed_bootstrap")
    (
        iqfeed_bootstrap_manifest_path,
        iqfeed_bootstrap_raw,
        iqfeed_bootstrap_manifest_sha256,
    ) = _stable_read(
        bootstrap_ref.get("path"),
        expected_sha256=bootstrap_ref.get("sha256"),
        roots=roots,
        field="iqfeed_bootstrap",
        max_bytes=_MAX_MANIFEST_BYTES,
    )
    iqfeed_bootstrap_document = _strict_json(
        iqfeed_bootstrap_raw, "iqfeed_bootstrap"
    )
    if (
        iqfeed_bootstrap_document.get("schema_version")
        != IQFEED_BOOTSTRAP_MANIFEST_SCHEMA_VERSION
    ):
        raise CapturedPaperActivationContractError(
            "IQFEED_BOOTSTRAP_SCHEMA_MISMATCH",
            "IQFeed bootstrap manifest schema is unsupported",
        )

    preactivation_ref: Mapping[str, Any] | None = None
    preactivation_manifest_sha256: str | None = None
    if is_activation:
        preactivation_ref = _mapping(
            document.get("preactivation_binding"), "preactivation_binding"
        )
        _exact_keys(
            preactivation_ref, {"path", "sha256"}, "preactivation_binding"
        )
        preactivation_manifest_sha256 = _sha(
            preactivation_ref.get("sha256"), "preactivation_binding.sha256"
        )

    receipt_refs = _mapping(document.get("readiness_receipts"), "readiness_receipts")
    readiness_cutover = _mapping(document.get("cutover"), "cutover")
    launcher_argument_contract_sha256 = _sha(
        readiness_cutover.get("launcher_arguments_sha256"),
        "cutover.launcher_arguments_sha256",
    )
    required_receipt_kinds = set(_RECEIPT_MAX_AGE_SECONDS)
    if not is_activation:
        required_receipt_kinds.remove("no_order_smoke")
    _exact_keys(receipt_refs, required_receipt_kinds, "readiness_receipts")
    receipt_paths: dict[str, Path] = {}
    receipt_hashes: dict[str, str] = {}
    receipt_documents: dict[str, Mapping[str, Any]] = {}
    for kind in sorted(required_receipt_kinds):
        ref = _mapping(receipt_refs.get(kind), f"readiness_receipts.{kind}")
        _exact_keys(ref, {"path", "sha256"}, f"readiness_receipts.{kind}")
        path, receipt_raw, digest = _stable_read(
            ref.get("path"),
            expected_sha256=ref.get("sha256"),
            roots=roots,
            field=f"readiness_receipts.{kind}",
        )
        receipt_doc = _strict_json(receipt_raw, f"readiness_receipts.{kind}")
        receipt_validation_now = now
        if kind in _superseded_readiness_kinds:
            # The final no-order receipt embeds the replacement authority.  We
            # still fully rehash and semantically revalidate the original
            # pre-smoke receipt, but its old wall-clock freshness is no longer
            # treated as current authority.
            receipt_validation_now = generated_at
        _validate_receipt(
            kind=kind,
            document=receipt_doc,
            now=receipt_validation_now,
            activation_generation=generation,
            expected_account_id=expected_account_id,
            code_build_sha256=code_build_sha256,
            effective_config_sha256=effective_config_sha256,
            capture_receipt_sha256=capture_receipt_sha256,
            preactivation_manifest_sha256=preactivation_manifest_sha256,
            runtime_environment_sha256=str(
                runtime.get("runtime_environment_sha256") or ""
            ),
            database_target_fingerprint=str(
                runtime.get("database_target_fingerprint") or ""
            ),
            iqfeed_bootstrap_manifest_sha256=(
                iqfeed_bootstrap_manifest_sha256
            ),
            launcher_argument_contract_sha256=(
                launcher_argument_contract_sha256
            ),
            capture_store_root=str(document.get("capture_store_root") or ""),
            source_hashes=source_hashes,
            allowed_read_roots=roots,
            require_artifact_probe=not (
                is_activation and kind in {"broker_account", "kill_switch"}
            ),
        )
        receipt_captured_at = _parse_utc(
            receipt_doc.get("captured_at"),
            f"readiness_receipts.{kind}.captured_at",
        )
        if receipt_captured_at > generated_at:
            raise CapturedPaperActivationContractError(
                "RECEIPT_CHRONOLOGY_INVALID",
                f"{kind} readiness receipt postdates its manifest",
            )
        receipt_paths[kind] = path
        receipt_hashes[kind] = digest
        receipt_documents[kind] = receipt_doc

    cutover = _mapping(document.get("cutover"), "cutover")
    _exact_keys(
        cutover,
        {
            "activation_artifact_root",
            "candidate_root",
            "host_ready_receipt_base",
            "launcher_source_path",
            "launcher_source_sha256",
            "launcher_path",
            "launcher_sha256",
            "stage0_source_path",
            "stage0_source_sha256",
            "stage0_path",
            "stage0_sha256",
            "service_source_path",
            "service_source_sha256",
            "service_path",
            "service_sha256",
            "launcher_arguments_path",
            "launcher_arguments_sha256",
            "python_executable_path",
            "python_executable_sha256",
            "python_dependency_root",
            "python_dependency_root_identity_sha256",
            "python_import_root",
            "scheduled_tasks",
            "singleton_policy",
            "rollback_required",
        },
        "cutover",
    )
    declared_root = Path(str(cutover.get("candidate_root") or "")).resolve(strict=True)
    if declared_root != root:
        raise CapturedPaperActivationContractError(
            "CUTOVER_ROOT_MISMATCH", "cutover candidate root mismatch"
        )
    import_root = Path(str(cutover.get("python_import_root") or ""))
    if not _is_local_absolute(import_root):
        raise CapturedPaperActivationContractError(
            "PYTHON_IMPORT_ROOT_INVALID", "Python import root must be absolute and local"
        )
    import_root = import_root.resolve(strict=True)
    _reject_reparse_chain(import_root)
    _reject_network_drive(import_root)
    if import_root != root:
        raise CapturedPaperActivationContractError(
            "PYTHON_IMPORT_ROOT_INVALID",
            "Python import root differs from the sealed candidate root",
        )
    python_path, _python_raw, python_sha = _stable_read(
        cutover.get("python_executable_path"),
        expected_sha256=cutover.get("python_executable_sha256"),
        roots=roots,
        field="cutover.python_executable",
    )
    dependency_root = Path(str(cutover.get("python_dependency_root") or ""))
    if not _is_local_absolute(dependency_root):
        raise CapturedPaperActivationContractError(
            "PYTHON_DEPENDENCY_ROOT_INVALID",
            "Python dependency root must be absolute and local",
        )
    dependency_root = dependency_root.resolve(strict=True)
    _reject_reparse_chain(dependency_root)
    _reject_network_drive(dependency_root)
    if not dependency_root.is_dir() or not _inside(dependency_root, roots):
        raise CapturedPaperActivationContractError(
            "PYTHON_DEPENDENCY_ROOT_INVALID",
            "Python dependency root escaped allowed roots",
        )
    if python_dependency_root_identity_sha256(
        dependency_root=dependency_root,
        python_executable=python_path,
        python_executable_sha256=python_sha,
    ) != _sha(
        cutover.get("python_dependency_root_identity_sha256"),
        "cutover.python_dependency_root_identity_sha256",
    ):
        raise CapturedPaperActivationContractError(
            "PYTHON_DEPENDENCY_ROOT_IDENTITY_MISMATCH",
            "Python dependency root identity changed",
        )
    artifact_root = Path(str(cutover.get("activation_artifact_root") or ""))
    if not _is_local_absolute(artifact_root):
        raise CapturedPaperActivationContractError(
            "ACTIVATION_ARTIFACT_ROOT_INVALID",
            "activation artifact root must be absolute and local",
        )
    artifact_root = artifact_root.resolve(strict=True)
    _reject_reparse_chain(artifact_root)
    _reject_network_drive(artifact_root)
    if not artifact_root.is_dir() or not _inside(artifact_root, roots):
        raise CapturedPaperActivationContractError(
            "ACTIVATION_ARTIFACT_ROOT_INVALID",
            "activation artifact root escaped allowed roots",
        )
    launcher_source_path, launcher_source_bytes, launcher_source_sha = _stable_read(
        cutover.get("launcher_source_path"),
        expected_sha256=cutover.get("launcher_source_sha256"),
        roots=(root,),
        field="cutover.launcher_source",
    )
    launcher_path, launcher_bytes, launcher_sha = _stable_read(
        cutover.get("launcher_path"),
        expected_sha256=cutover.get("launcher_sha256"),
        roots=(artifact_root,),
        field="cutover.launcher",
    )
    service_source_path, service_source_bytes, service_source_sha = _stable_read(
        cutover.get("service_source_path"),
        expected_sha256=cutover.get("service_source_sha256"),
        roots=(root,),
        field="cutover.service_source",
    )
    service_path, service_bytes, service_sha = _stable_read(
        cutover.get("service_path"),
        expected_sha256=cutover.get("service_sha256"),
        roots=(artifact_root,),
        field="cutover.service",
    )
    stage0_source_path, stage0_source_bytes, stage0_source_sha = _stable_read(
        cutover.get("stage0_source_path"),
        expected_sha256=cutover.get("stage0_source_sha256"),
        roots=(root,),
        field="cutover.stage0_source",
    )
    stage0_path, stage0_bytes, stage0_sha = _stable_read(
        cutover.get("stage0_path"),
        expected_sha256=cutover.get("stage0_sha256"),
        roots=(artifact_root,),
        field="cutover.stage0",
    )
    host_ready = _strict_local_output_path(
        cutover.get("host_ready_receipt_base"),
        roots=(artifact_root,),
        field="cutover.host_ready_receipt_base",
    )
    if (
        launcher_source_path != source_paths["activation_launcher"]
        or launcher_source_sha != source_hashes["activation_launcher"]
        or launcher_sha != launcher_source_sha
        or launcher_bytes != launcher_source_bytes
        or service_source_path != source_paths["activation_service"]
        or service_source_sha != source_hashes["activation_service"]
        or service_sha != service_source_sha
        or service_bytes != service_source_bytes
        or stage0_source_path != source_paths["activation_stage0"]
        or stage0_source_sha != source_hashes["activation_stage0"]
        or stage0_sha != stage0_source_sha
        or stage0_bytes != stage0_source_bytes
        or launcher_path.name.casefold() != f"{launcher_sha}.ps1"
        or service_path.name.casefold() != f"{service_sha}.py"
        or stage0_path.name.casefold() != f"{stage0_sha}.py"
        or launcher_path.parent.name.casefold() != launcher_sha
        or service_path.parent.name.casefold() != service_sha
        or stage0_path.parent.name.casefold() != stage0_sha
        or launcher_path.parent.parent.name.casefold() != generation
        or service_path.parent.parent.name.casefold() != generation
        or stage0_path.parent.parent.name.casefold() != generation
        or launcher_path.parent.parent.parent != artifact_root
        or service_path.parent.parent.parent != artifact_root
        or stage0_path.parent.parent.parent != artifact_root
        or host_ready.parent.name.casefold() != "handshake"
        or host_ready.parent.parent.name.casefold() != generation
        or host_ready.parent.parent.parent != artifact_root
        or cutover.get("singleton_policy") != "one_unified_candidate_host"
        or cutover.get("rollback_required") is not True
    ):
        raise CapturedPaperActivationContractError(
            "CUTOVER_BINDING_MISMATCH", "cutover launcher/singleton/rollback binding mismatch"
        )
    (
        launcher_arguments_path,
        launcher_arguments_raw,
        launcher_arguments_sha,
    ) = _stable_read(
        cutover.get("launcher_arguments_path"),
        expected_sha256=cutover.get("launcher_arguments_sha256"),
        roots=roots,
        field="cutover.launcher_arguments",
        max_bytes=_MAX_MANIFEST_BYTES,
    )
    launcher_arguments_document = _strict_json(
        launcher_arguments_raw, "cutover.launcher_arguments"
    )
    launcher_invocations = _validate_launcher_argument_contract(
        launcher_arguments_document,
        raw=launcher_arguments_raw,
        candidate_root=root,
        allowed_read_roots=roots,
        source_paths=source_paths,
        source_hashes=source_hashes,
        activation_generation=generation,
    )
    activate_projection = launcher_invocations["ActivatePaper"]
    if not (
        Path(str(activate_projection.get("launcher_path"))) == launcher_path
        and Path(str(activate_projection.get("service_staged_path"))) == service_path
        and Path(str(activate_projection.get("host_ready_receipt_base"))) == host_ready
        and Path(str(activate_projection.get("python_import_root"))) == import_root
    ):
        raise CapturedPaperActivationContractError(
            "CUTOVER_BINDING_MISMATCH",
            "cutover entrypoints differ from the sealed launcher projection",
        )
    tasks = cutover.get("scheduled_tasks")
    if (
        not isinstance(tasks, list)
        or len(tasks) != len(set(tasks))
        or set(tasks) != _REQUIRED_TASKS
    ):
        raise CapturedPaperActivationContractError(
            "CUTOVER_TASK_MISMATCH", "cutover must bind exactly the four IQFeed tasks"
        )

    capture_store = Path(str(document.get("capture_store_root") or ""))
    if not _is_local_absolute(capture_store):
        raise CapturedPaperActivationContractError(
            "CAPTURE_STORE_INVALID", "capture store must be an absolute local path"
        )
    capture_store = capture_store.resolve(strict=True)
    _reject_reparse_chain(capture_store)
    if not capture_store.is_dir() or not _inside(capture_store, roots):
        raise CapturedPaperActivationContractError(
            "CAPTURE_STORE_INVALID", "capture store escaped the allowed roots"
        )

    if is_activation:
        if preactivation_ref is None:
            raise CapturedPaperActivationContractError(
                "PREACTIVATION_BINDING_MISSING",
                "final activation omitted its preactivation binding",
            )
        preactivation = _load_captured_paper_envelope(
            preactivation_ref.get("path"),
            expected_manifest_sha256=preactivation_ref.get("sha256"),
            candidate_root=root,
            allowed_read_roots=roots,
            envelope_stage="preactivation",
            wall_clock=lambda: now,
            _superseded_readiness_kinds=frozenset(
                {"broker_account", "kill_switch"}
            ),
        )
        shared_fields = (
            "activation_generation",
            "runtime_environment",
            "code_build",
            "capture_binding",
            "iqfeed_bootstrap",
            "cutover",
            "capture_store_root",
        )
        if any(
            _canonical_json_bytes(document.get(field))
            != _canonical_json_bytes(preactivation.manifest.get(field))
            for field in shared_fields
        ):
            raise CapturedPaperActivationContractError(
                "PREACTIVATION_BINDING_MISMATCH",
                "final activation changed material already sealed for no-order smoke",
            )
        final_boundary = dict(boundary)
        preactivation_boundary = dict(
            _mapping(
                preactivation.manifest.get("authority_boundary"),
                "preactivation.authority_boundary",
            )
        )
        final_boundary.pop("paper_order_submission_authorized", None)
        preactivation_boundary.pop("paper_order_submission_authorized", None)
        if final_boundary != preactivation_boundary:
            raise CapturedPaperActivationContractError(
                "PREACTIVATION_BINDING_MISMATCH",
                "final activation changed the preactivation authority boundary",
            )
        preactivation_receipts = _mapping(
            preactivation.manifest.get("readiness_receipts"),
            "preactivation.readiness_receipts",
        )
        refreshed_kinds = {"broker_account", "kill_switch"}
        final_prior_receipts = {
            kind: value
            for kind, value in receipt_refs.items()
            if kind not in refreshed_kinds | {"no_order_smoke"}
        }
        preactivation_stable_receipts = {
            kind: value
            for kind, value in preactivation_receipts.items()
            if kind not in refreshed_kinds
        }
        if _canonical_json_bytes(final_prior_receipts) != _canonical_json_bytes(
            preactivation_stable_receipts
        ):
            raise CapturedPaperActivationContractError(
                "PREACTIVATION_BINDING_MISMATCH",
                "final activation replaced non-refreshable preactivation evidence",
            )
        refreshed_documents = _mapping(
            receipt_documents["no_order_smoke"].get("refreshed_readiness"),
            "no_order_smoke.refreshed_readiness",
        )
        if any(
            _canonical_json_bytes(receipt_documents[kind])
            != _canonical_json_bytes(refreshed_documents.get(kind))
            for kind in refreshed_kinds
        ):
            raise CapturedPaperActivationContractError(
                "POST_SMOKE_READINESS_MISMATCH",
                "final broker/kill receipts are not the nested post-smoke authority",
            )
        for kind in refreshed_kinds:
            refreshed_path = receipt_paths[kind]
            refreshed_digest = receipt_hashes[kind]
            if refreshed_path.name != f"{refreshed_digest}.json":
                raise CapturedPaperActivationContractError(
                    "POST_SMOKE_READINESS_NOT_CONTENT_ADDRESSED",
                    f"final {kind} readiness path is not content-addressed",
                )
        no_order_captured_at = _parse_utc(
            receipt_documents["no_order_smoke"].get("captured_at"),
            "no_order_smoke.captured_at",
        )
        if (
            preactivation.generated_at > no_order_captured_at
            or no_order_captured_at > generated_at
            or generated_at < preactivation.generated_at
            or expires_at > preactivation.expires_at
        ):
            raise CapturedPaperActivationContractError(
                "PREACTIVATION_CHRONOLOGY_INVALID",
                "no-order smoke/final activation chronology escaped the preactivation window",
            )

    verified_type = (
        VerifiedCapturedPaperActivation
        if is_activation
        else VerifiedCapturedPaperPreactivation
    )
    return verified_type(
        manifest_path=manifest_file,
        manifest_sha256=manifest_sha,
        activation_generation=generation,
        expected_account_id=expected_account_id,
        code_build_sha256=code_build_sha256,
        effective_config_sha256=effective_config_sha256,
        capture_receipt_sha256=capture_receipt_sha256,
        source_paths=MappingProxyType(source_paths),
        source_hashes=MappingProxyType(source_hashes),
        receipt_paths=MappingProxyType(receipt_paths),
        receipt_hashes=MappingProxyType(receipt_hashes),
        launcher_path=launcher_path,
        launcher_sha256=launcher_sha,
        candidate_root=root,
        capture_store_root=capture_store,
        iqfeed_bootstrap_manifest_path=iqfeed_bootstrap_manifest_path,
        iqfeed_bootstrap_manifest_sha256=iqfeed_bootstrap_manifest_sha256,
        generated_at=generated_at,
        expires_at=expires_at,
        manifest=MappingProxyType(dict(document)),
        envelope_stage=envelope_stage,
        paper_order_submission_authorized=is_activation,
    )


def load_captured_paper_preactivation(
    manifest_path: str | Path,
    *,
    expected_manifest_sha256: str,
    candidate_root: str | Path,
    allowed_read_roots: Sequence[str | Path],
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> VerifiedCapturedPaperPreactivation:
    """Verify the no-order stage; this stage structurally denies broker POSTs."""

    verified = _load_captured_paper_envelope(
        manifest_path,
        expected_manifest_sha256=expected_manifest_sha256,
        candidate_root=candidate_root,
        allowed_read_roots=allowed_read_roots,
        envelope_stage="preactivation",
        wall_clock=wall_clock,
    )
    if not isinstance(verified, VerifiedCapturedPaperPreactivation):
        raise CapturedPaperActivationContractError(
            "ENVELOPE_STAGE_INVALID", "preactivation verifier returned the wrong type"
        )
    return verified


def load_captured_paper_activation(
    manifest_path: str | Path,
    *,
    expected_manifest_sha256: str,
    candidate_root: str | Path,
    allowed_read_roots: Sequence[str | Path],
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> VerifiedCapturedPaperActivation:
    """Verify the final PAPER-only order authority with local reads only."""

    verified = _load_captured_paper_envelope(
        manifest_path,
        expected_manifest_sha256=expected_manifest_sha256,
        candidate_root=candidate_root,
        allowed_read_roots=allowed_read_roots,
        envelope_stage="activation",
        wall_clock=wall_clock,
    )
    if isinstance(verified, VerifiedCapturedPaperPreactivation):
        raise CapturedPaperActivationContractError(
            "ENVELOPE_STAGE_INVALID", "activation verifier returned no-order authority"
        )
    return verified


def _publish_content_addressed_json(
    output_root: Path, document: Mapping[str, Any]
) -> tuple[Path, str]:
    raw = _canonical_json_bytes(document)
    digest = hashlib.sha256(raw).hexdigest()
    parent = output_root / digest[:2]
    parent.mkdir(parents=True, exist_ok=True)
    _reject_reparse_chain(parent)
    path = parent / f"{digest}.json"
    try:
        with path.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        _existing_path, existing_raw, _existing_digest = _stable_read(
            path,
            expected_sha256=digest,
            roots=(output_root,),
            field="published_activation_manifest",
            max_bytes=_MAX_MANIFEST_BYTES,
        )
        if existing_raw != raw:
            raise CapturedPaperActivationContractError(
                "CONTENT_ADDRESS_COLLISION",
                "activation object path exists with different bytes",
            )
    _stable_read(
        path,
        expected_sha256=digest,
        roots=(output_root,),
        field="published_activation_manifest",
        max_bytes=_MAX_MANIFEST_BYTES,
    )
    return path, digest


def finalize_captured_paper_activation(
    preactivation: VerifiedCapturedPaperPreactivation,
    *,
    no_order_smoke_path: str | Path,
    no_order_smoke_sha256: str,
    output_root: str | Path,
    allowed_read_roots: Sequence[str | Path],
    generated_at: datetime | None = None,
    expires_at: datetime | None = None,
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> BuiltCapturedPaperActivation:
    """Promote one verified no-order run into final fake-money PAPER authority.

    This function performs local reads/writes only.  It cannot contact Alpaca,
    IQFeed, a database, Task Scheduler, or any service process.
    """

    if not isinstance(preactivation, VerifiedCapturedPaperPreactivation):
        raise CapturedPaperActivationContractError(
            "PREACTIVATION_REQUIRED",
            "finalization requires typed no-order preactivation authority",
        )
    roots = _roots(allowed_read_roots)
    now = wall_clock()
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise CapturedPaperActivationContractError(
            "INVALID_CLOCK", "finalization wall clock is not timezone-aware"
        )
    now = now.astimezone(UTC)
    sealed = _load_captured_paper_envelope(
        preactivation.manifest_path,
        expected_manifest_sha256=preactivation.manifest_sha256,
        candidate_root=preactivation.candidate_root,
        allowed_read_roots=roots,
        envelope_stage="preactivation",
        wall_clock=lambda: now,
        _superseded_readiness_kinds=frozenset(
            {"broker_account", "kill_switch"}
        ),
    )
    if not isinstance(sealed, VerifiedCapturedPaperPreactivation):
        raise CapturedPaperActivationContractError(
            "ENVELOPE_STAGE_INVALID",
            "sealed preactivation verifier returned the wrong type",
        )
    sealed_cutover = _mapping(sealed.manifest.get("cutover"), "cutover")
    host_ready_base = _strict_local_output_path(
        sealed_cutover.get("host_ready_receipt_base"),
        roots=roots,
        field="cutover.host_ready_receipt_base",
    )
    if any(
        path.exists()
        for path in (
            host_ready_base,
            host_ready_base.with_name(host_ready_base.name + ".permit.json"),
            host_ready_base.with_name(host_ready_base.name + ".started.json"),
            host_ready_base.with_name(host_ready_base.name + ".revoked.json"),
        )
    ):
        raise CapturedPaperActivationContractError(
            "HOST_READY_RECEIPT_ALREADY_EXISTS",
            "final activation requires a new generation-owned host handshake",
        )
    no_order_path, no_order_raw, no_order_digest = _stable_read(
        no_order_smoke_path,
        expected_sha256=no_order_smoke_sha256,
        roots=roots,
        field="readiness_receipts.no_order_smoke",
    )
    no_order_document = _strict_json(no_order_raw, "readiness_receipts.no_order_smoke")
    _validate_receipt(
        kind="no_order_smoke",
        document=no_order_document,
        now=now,
        activation_generation=sealed.activation_generation,
        expected_account_id=sealed.expected_account_id,
        code_build_sha256=sealed.code_build_sha256,
        effective_config_sha256=sealed.effective_config_sha256,
        capture_receipt_sha256=sealed.capture_receipt_sha256,
        preactivation_manifest_sha256=sealed.manifest_sha256,
        runtime_environment_sha256=str(
            _mapping(
                sealed.manifest.get("runtime_environment"),
                "runtime_environment",
            ).get("runtime_environment_sha256")
            or ""
        ),
        database_target_fingerprint=str(
            _mapping(
                sealed.manifest.get("runtime_environment"),
                "runtime_environment",
            ).get("database_target_fingerprint")
            or ""
        ),
        iqfeed_bootstrap_manifest_sha256=(
            sealed.iqfeed_bootstrap_manifest_sha256
        ),
        launcher_argument_contract_sha256=str(
            _mapping(sealed.manifest.get("cutover"), "cutover").get(
                "launcher_arguments_sha256"
            )
            or ""
        ),
        capture_store_root=str(sealed.capture_store_root),
        source_hashes=sealed.source_hashes,
        allowed_read_roots=roots,
    )
    final_generated_at = (generated_at or now)
    if not isinstance(final_generated_at, datetime) or final_generated_at.tzinfo is None:
        raise CapturedPaperActivationContractError(
            "INVALID_CLOCK", "final activation generated_at is not timezone-aware"
        )
    final_generated_at = final_generated_at.astimezone(UTC)
    no_order_expires_at = _parse_utc(
        no_order_document.get("expires_at"), "no_order_smoke.expires_at"
    )
    final_expires_at = expires_at or min(
        sealed.expires_at,
        no_order_expires_at,
        final_generated_at + timedelta(seconds=_MAX_MANIFEST_AGE_SECONDS),
    )
    if not isinstance(final_expires_at, datetime) or final_expires_at.tzinfo is None:
        raise CapturedPaperActivationContractError(
            "INVALID_CLOCK", "final activation expires_at is not timezone-aware"
        )
    final_expires_at = final_expires_at.astimezone(UTC)
    no_order_captured_at = _parse_utc(
        no_order_document.get("captured_at"), "no_order_smoke.captured_at"
    )
    if (
        final_generated_at < no_order_captured_at
        or final_generated_at < sealed.generated_at
        or final_expires_at > sealed.expires_at
        or final_expires_at > no_order_expires_at
        or final_expires_at <= final_generated_at
    ):
        raise CapturedPaperActivationContractError(
            "PREACTIVATION_CHRONOLOGY_INVALID",
            "final activation is outside the sealed no-order window",
        )
    destination = Path(output_root)
    if not _is_local_absolute(destination):
        raise CapturedPaperActivationContractError(
            "NONLOCAL_ROOT", "activation output root must be absolute and local"
        )
    destination = destination.resolve(strict=True)
    _reject_reparse_chain(destination)
    if not destination.is_dir() or not _inside(destination, roots):
        raise CapturedPaperActivationContractError(
            "PATH_OUTSIDE_ROOT", "activation output root escaped the allowed roots"
        )

    final_document = json.loads(
        _canonical_json_bytes(dict(sealed.manifest)).decode("utf-8")
    )
    final_document["schema_version"] = ACTIVATION_MANIFEST_SCHEMA_VERSION
    final_document["generated_at"] = _iso(final_generated_at)
    final_document["expires_at"] = _iso(final_expires_at)
    final_document["authority_boundary"]["paper_order_submission_authorized"] = True
    refreshed_readiness = _mapping(
        no_order_document.get("refreshed_readiness"),
        "no_order_smoke.refreshed_readiness",
    )
    _exact_keys(
        refreshed_readiness,
        {"broker_account", "kill_switch"},
        "no_order_smoke.refreshed_readiness",
    )
    for kind in ("broker_account", "kill_switch"):
        refreshed_document = _mapping(
            refreshed_readiness.get(kind),
            f"no_order_smoke.refreshed_readiness.{kind}",
        )
        refreshed_path, refreshed_digest = _publish_content_addressed_json(
            destination, refreshed_document
        )
        final_document["readiness_receipts"][kind] = {
            "path": str(refreshed_path),
            "sha256": refreshed_digest,
        }
    final_document["readiness_receipts"]["no_order_smoke"] = {
        "path": str(no_order_path),
        "sha256": no_order_digest,
    }
    final_document["preactivation_binding"] = {
        "path": str(sealed.manifest_path),
        "sha256": sealed.manifest_sha256,
    }
    final_document.pop("activation_manifest_sha256", None)
    final_document["activation_manifest_sha256"] = sha256_json(final_document)
    final_path, final_digest = _publish_content_addressed_json(
        destination, final_document
    )
    verified = load_captured_paper_activation(
        final_path,
        expected_manifest_sha256=final_digest,
        candidate_root=sealed.candidate_root,
        allowed_read_roots=roots,
        wall_clock=lambda: now,
    )
    return BuiltCapturedPaperActivation(
        manifest_path=final_path,
        manifest_sha256=final_digest,
        preactivation_manifest_sha256=sealed.manifest_sha256,
        no_order_smoke_sha256=no_order_digest,
        verified=verified,
    )


__all__ = [
    "ACTIVATION_MANIFEST_SCHEMA_VERSION",
    "PREACTIVATION_MANIFEST_SCHEMA_VERSION",
    "CAPTURE_BINDING_SCHEMA_VERSION",
    "CODE_BUILD_SCHEMA_VERSION",
    "IQFEED_BOOTSTRAP_MANIFEST_SCHEMA_VERSION",
    "LAUNCHER_ARGUMENT_CONTRACT_SCHEMA_VERSION",
    "LAUNCHER_INVOCATION_PROJECTION_SCHEMA_VERSION",
    "CapturedPaperActivationContractError",
    "BuiltCapturedPaperActivation",
    "VerifiedCapturedPaperActivation",
    "VerifiedCapturedPaperPreactivation",
    "finalize_captured_paper_activation",
    "launcher_invocation_projection",
    "load_captured_paper_activation",
    "load_captured_paper_preactivation",
    "sha256_json",
]
