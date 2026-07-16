from __future__ import annotations

import ast
import dataclasses
import difflib
from pathlib import Path
from typing import Callable, Mapping, Sequence


@dataclasses.dataclass(frozen=True)
class WorkflowSkillPatch:
    skill_id: str
    milestone: int
    diff: str
    changed_files: tuple[str, ...]
    evidence: Mapping[str, object]


WorkflowBuilder = Callable[[Mapping[str, str], str, int], Mapping[str, str] | None]


def _parse(path: str, content: str) -> ast.Module | None:
    try:
        return ast.parse(content, filename=path)
    except SyntaxError:
        return None


def _defines(tree: ast.Module, *, function: str = "", class_name: str = "") -> bool:
    for node in tree.body:
        if function and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == function:
                return True
        if class_name and isinstance(node, ast.ClassDef) and node.name == class_name:
            return True
    return False


def _unique_symbol_path(
    files: Mapping[str, str],
    *,
    function: str = "",
    class_name: str = "",
) -> str | None:
    matches = []
    for path, content in files.items():
        tree = _parse(path, content)
        if tree is not None and _defines(tree, function=function, class_name=class_name):
            matches.append(path)
    return matches[0] if len(matches) == 1 else None


def _module(path: str) -> str:
    normalized = str(Path(path).with_suffix("")).replace("\\", "/")
    return normalized.replace("/", ".")


def _milestone(prompt: str) -> int | None:
    lower = prompt.lower()
    for value in (1, 2, 3):
        markers = (
            f"milestone {value}/3",
            f"milestone {value} of 3",
            f"phase {value}/3",
            f"phase {value} of 3",
        )
        if any(marker in lower for marker in markers):
            return value
    return None


def _clean(value: str) -> str:
    return value.strip("\n") + "\n"


def _diff(path: str, before: str, after: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
    ) + "\n"


def _rollout_workflow(
    files: Mapping[str, str],
    prompt: str,
    milestone: int,
) -> Mapping[str, str] | None:
    lower = prompt.lower()
    markers = ("rolloutconfig", "stable_bucket", "decide_rollout", "evaluate_request")
    if not all(marker in lower for marker in markers):
        return None
    config_path = _unique_symbol_path(files, class_name="RolloutConfig")
    cohort_path = _unique_symbol_path(files, function="stable_bucket")
    decision_path = _unique_symbol_path(files, function="decide_rollout")
    service_path = _unique_symbol_path(files, function="evaluate_request")
    paths = (config_path, cohort_path, decision_path, service_path)
    if any(path is None for path in paths) or len(set(paths)) != 4:
        return None
    assert config_path and cohort_path and decision_path and service_path
    config_module = _module(config_path)
    cohort_module = _module(cohort_path)
    decision_module = _module(decision_path)

    if milestone == 1:
        config = '''
from dataclasses import dataclass
from typing import Mapping


def _parse_bool(value: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("invalid rollout config")


@dataclass(frozen=True)
class RolloutConfig:
    enabled: bool = False
    percentage: int = 0
    salt: str = "default"

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "RolloutConfig":
        try:
            enabled = _parse_bool(values.get("ENABLED", "false"))
            percentage = int(values.get("PERCENTAGE", "0"))
            salt = str(values.get("SALT", "default")).strip()
        except (TypeError, ValueError):
            raise ValueError("invalid rollout config") from None
        if percentage < 0 or percentage > 100 or not salt:
            raise ValueError("invalid rollout config")
        return cls(enabled=enabled, percentage=percentage, salt=salt)
'''
        decision = f'''
from typing import Any

from {cohort_module} import stable_bucket
from {config_module} import RolloutConfig


def decide_rollout(subject_id: str, config: RolloutConfig) -> dict[str, Any]:
    if not config.enabled:
        return {{"enabled": False, "reason": "disabled", "bucket": None}}
    bucket = stable_bucket(subject_id, config.salt)
    enabled = bucket < config.percentage
    return {{
        "enabled": enabled,
        "reason": "cohort" if enabled else "outside_cohort",
        "bucket": bucket,
    }}
'''
        service = f'''
from typing import Any, Mapping

from {config_module} import RolloutConfig
from {decision_module} import decide_rollout


def evaluate_request(subject_id: str, raw_config: Mapping[str, str]) -> dict[str, Any]:
    return decide_rollout(subject_id, RolloutConfig.from_mapping(raw_config))
'''
    elif milestone == 2:
        config = '''
from dataclasses import dataclass
from typing import Mapping


def _parse_bool(value: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("invalid rollout config")


def _parse_subjects(value: str) -> frozenset[str]:
    return frozenset(item.strip() for item in str(value).split(",") if item.strip())


@dataclass(frozen=True)
class RolloutConfig:
    enabled: bool = False
    percentage: int = 0
    salt: str = "default"
    allowlist: frozenset[str] = frozenset()
    denylist: frozenset[str] = frozenset()

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "RolloutConfig":
        try:
            enabled = _parse_bool(values.get("ENABLED", "false"))
            percentage = int(values.get("PERCENTAGE", "0"))
            salt = str(values.get("SALT", "default")).strip()
            allowlist = _parse_subjects(values.get("ALLOWLIST", ""))
            denylist = _parse_subjects(values.get("DENYLIST", ""))
        except (TypeError, ValueError):
            raise ValueError("invalid rollout config") from None
        if percentage < 0 or percentage > 100 or not salt or allowlist & denylist:
            raise ValueError("invalid rollout config")
        return cls(enabled, percentage, salt, allowlist, denylist)
'''
        decision = f'''
from typing import Any

from {cohort_module} import stable_bucket
from {config_module} import RolloutConfig


def decide_rollout(subject_id: str, config: RolloutConfig) -> dict[str, Any]:
    if subject_id in config.denylist:
        return {{"enabled": False, "reason": "denylist", "bucket": None}}
    if not config.enabled:
        return {{"enabled": False, "reason": "disabled", "bucket": None}}
    if subject_id in config.allowlist:
        return {{"enabled": True, "reason": "allowlist", "bucket": None}}
    bucket = stable_bucket(subject_id, config.salt)
    enabled = bucket < config.percentage
    return {{
        "enabled": enabled,
        "reason": "cohort" if enabled else "outside_cohort",
        "bucket": bucket,
    }}
'''
        service = f'''
from typing import Any, Mapping

from {config_module} import RolloutConfig
from {decision_module} import decide_rollout


def evaluate_request(
    subject_id: str,
    raw_config: Mapping[str, str],
    audit: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    decision = decide_rollout(subject_id, RolloutConfig.from_mapping(raw_config))
    if audit is not None:
        audit.append({{"subject_id": subject_id, **decision}})
    return decision
'''
    else:
        config = '''
from dataclasses import dataclass
from typing import Mapping


def _parse_bool(value: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("invalid rollout config")


def _parse_subjects(value: str) -> frozenset[str]:
    return frozenset(item.strip() for item in str(value).split(",") if item.strip())


@dataclass(frozen=True)
class RolloutConfig:
    enabled: bool = False
    percentage: int = 0
    salt: str = "default"
    allowlist: frozenset[str] = frozenset()
    denylist: frozenset[str] = frozenset()
    kill_switch: bool = False
    config_version: int = 1

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "RolloutConfig":
        try:
            enabled = _parse_bool(values.get("ENABLED", "false"))
            percentage = int(values.get("PERCENTAGE", "0"))
            salt = str(values.get("SALT", "default")).strip()
            allowlist = _parse_subjects(values.get("ALLOWLIST", ""))
            denylist = _parse_subjects(values.get("DENYLIST", ""))
            kill_switch = _parse_bool(values.get("KILL_SWITCH", "false"))
            config_version = int(values.get("CONFIG_VERSION", "1"))
        except (TypeError, ValueError):
            raise ValueError("invalid rollout config") from None
        if (
            percentage < 0
            or percentage > 100
            or not salt
            or allowlist & denylist
            or config_version < 1
        ):
            raise ValueError("invalid rollout config")
        return cls(
            enabled,
            percentage,
            salt,
            allowlist,
            denylist,
            kill_switch,
            config_version,
        )
'''
        decision = f'''
import hashlib
from typing import Any

from {cohort_module} import stable_bucket
from {config_module} import RolloutConfig


def decide_rollout(subject_id: str, config: RolloutConfig) -> dict[str, Any]:
    bucket: int | None = None
    if config.kill_switch:
        enabled, reason = False, "kill_switch"
    elif subject_id in config.denylist:
        enabled, reason = False, "denylist"
    elif not config.enabled:
        enabled, reason = False, "disabled"
    elif subject_id in config.allowlist:
        enabled, reason = True, "allowlist"
    else:
        bucket = stable_bucket(subject_id, config.salt)
        enabled = bucket < config.percentage
        reason = "cohort" if enabled else "outside_cohort"
    material = f"{{config.config_version}}|{{subject_id}}|{{reason}}|{{bucket}}"
    return {{
        "enabled": enabled,
        "reason": reason,
        "bucket": bucket,
        "config_version": config.config_version,
        "decision_id": hashlib.sha256(material.encode("utf-8")).hexdigest()[:16],
    }}
'''
        service = f'''
from collections import Counter
from typing import Any, Mapping, Sequence

from {config_module} import RolloutConfig
from {decision_module} import decide_rollout


def evaluate_request(
    subject_id: str,
    raw_config: Mapping[str, str],
    audit: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    decision = decide_rollout(subject_id, RolloutConfig.from_mapping(raw_config))
    if audit is not None:
        audit.append({{"subject_id": subject_id, **decision}})
    return decision


def summarize_decisions(audit: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts = Counter(str(item.get("reason", "unknown")) for item in audit)
    return dict(sorted(counts.items()))
'''

    cohort = '''
import hashlib


def stable_bucket(subject_id: str, salt: str) -> int:
    subject = str(subject_id).strip()
    normalized_salt = str(salt).strip()
    if not subject or not normalized_salt:
        raise ValueError("invalid rollout subject")
    digest = hashlib.sha256(f"{normalized_salt}:{subject}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % 100
'''
    return {
        config_path: _clean(config),
        cohort_path: _clean(cohort),
        decision_path: _clean(decision),
        service_path: _clean(service),
    }


def _import_workflow(
    files: Mapping[str, str],
    prompt: str,
    milestone: int,
) -> Mapping[str, str] | None:
    lower = prompt.lower()
    markers = ("importitem", "checkpointstore", "execute_handler", "run_import")
    if not all(marker in lower for marker in markers):
        return None
    model_path = _unique_symbol_path(files, class_name="ImportItem")
    store_path = _unique_symbol_path(files, class_name="CheckpointStore")
    processor_path = _unique_symbol_path(files, function="execute_handler")
    service_path = _unique_symbol_path(files, function="run_import")
    paths = (model_path, store_path, processor_path, service_path)
    if any(path is None for path in paths) or len(set(paths)) != 4:
        return None
    assert model_path and store_path and processor_path and service_path
    model_module = _module(model_path)
    store_module = _module(store_path)
    processor_module = _module(processor_path)

    model = '''
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ImportItem:
    item_id: str
    sequence: int
    payload: Mapping[str, Any]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ImportItem":
        try:
            item_id = str(value.get("item_id", "")).strip()
            sequence = int(value.get("sequence", 0))
            payload = value.get("payload") or {}
        except (TypeError, ValueError):
            raise ValueError("invalid import item") from None
        if not item_id or sequence < 1 or not isinstance(payload, Mapping):
            raise ValueError("invalid import item")
        return cls(item_id=item_id, sequence=sequence, payload=payload)
'''
    if milestone == 1:
        store = f'''
from {model_module} import ImportItem


class CheckpointStore:
    def __init__(self) -> None:
        self.checkpoints: dict[str, int] = {{}}
        self.processed: set[tuple[str, str]] = set()

    def checkpoint(self, job_id: str) -> int:
        return self.checkpoints.get(job_id, 0)

    def was_processed(self, job_id: str, item_id: str) -> bool:
        return (job_id, item_id) in self.processed

    def mark_processed(self, job_id: str, item: ImportItem) -> None:
        self.processed.add((job_id, item.item_id))
        self.checkpoints[job_id] = max(self.checkpoint(job_id), item.sequence)
'''
        processor = f'''
from collections.abc import Callable

from {model_module} import ImportItem


def execute_handler(item: ImportItem, handler: Callable[[ImportItem], None]) -> int:
    handler(item)
    return 0
'''
        service = f'''
from collections.abc import Callable, Sequence
from typing import Any

from {model_module} import ImportItem
from {processor_module} import execute_handler
from {store_module} import CheckpointStore


def _validate_batch(job_id: str, items: Sequence[ImportItem]) -> None:
    if not str(job_id).strip():
        raise ValueError("invalid import batch")
    sequences = [item.sequence for item in items]
    if sequences != sorted(sequences) or len(set(sequences)) != len(sequences):
        raise ValueError("invalid import batch")


def run_import(
    items: Sequence[ImportItem],
    job_id: str,
    store: CheckpointStore,
    handler: Callable[[ImportItem], None],
) -> dict[str, Any]:
    _validate_batch(job_id, items)
    processed = 0
    skipped = 0
    for item in items:
        if item.sequence <= store.checkpoint(job_id) or store.was_processed(job_id, item.item_id):
            skipped += 1
            continue
        execute_handler(item, handler)
        store.mark_processed(job_id, item)
        processed += 1
    return {{"processed": processed, "skipped": skipped, "checkpoint": store.checkpoint(job_id)}}
'''
    else:
        store = f'''
from typing import Any

from {model_module} import ImportItem


class CheckpointStore:
    def __init__(self) -> None:
        self.checkpoints: dict[str, int] = {{}}
        self.processed: set[tuple[str, str]] = set()
        self.dead_letters: list[dict[str, Any]] = []
        self.leases: dict[str, tuple[str, float]] = {{}}
        self.audit: list[dict[str, Any]] = []

    def checkpoint(self, job_id: str) -> int:
        return self.checkpoints.get(job_id, 0)

    def was_processed(self, job_id: str, item_id: str) -> bool:
        return (job_id, item_id) in self.processed

    def mark_processed(self, job_id: str, item: ImportItem) -> None:
        self.processed.add((job_id, item.item_id))
        self.checkpoints[job_id] = max(self.checkpoint(job_id), item.sequence)

    def dead_letter(self, job_id: str, item: ImportItem, reason: str) -> None:
        self.dead_letters.append({{"job_id": job_id, "item_id": item.item_id, "reason": reason}})
        self.checkpoints[job_id] = max(self.checkpoint(job_id), item.sequence)

    def acquire(self, job_id: str, owner: str, now: float, ttl: float) -> bool:
        if not owner or ttl <= 0:
            raise ValueError("invalid import lease")
        current = self.leases.get(job_id)
        if current is not None and current[0] != owner and current[1] > now:
            return False
        self.leases[job_id] = (owner, now + ttl)
        return True

    def release(self, job_id: str, owner: str) -> None:
        current = self.leases.get(job_id)
        if current is not None and current[0] == owner:
            self.leases.pop(job_id, None)
'''
        if milestone == 2:
            store = f'''
from typing import Any

from {model_module} import ImportItem


class CheckpointStore:
    def __init__(self) -> None:
        self.checkpoints: dict[str, int] = {{}}
        self.processed: set[tuple[str, str]] = set()
        self.dead_letters: list[dict[str, Any]] = []

    def checkpoint(self, job_id: str) -> int:
        return self.checkpoints.get(job_id, 0)

    def was_processed(self, job_id: str, item_id: str) -> bool:
        return (job_id, item_id) in self.processed

    def mark_processed(self, job_id: str, item: ImportItem) -> None:
        self.processed.add((job_id, item.item_id))
        self.checkpoints[job_id] = max(self.checkpoint(job_id), item.sequence)

    def dead_letter(self, job_id: str, item: ImportItem, reason: str) -> None:
        self.dead_letters.append({{"job_id": job_id, "item_id": item.item_id, "reason": reason}})
        self.checkpoints[job_id] = max(self.checkpoint(job_id), item.sequence)
'''
        processor = f'''
from collections.abc import Callable

from {model_module} import ImportItem


class TransientImportError(RuntimeError):
    pass


class PermanentImportError(RuntimeError):
    pass


def execute_handler(
    item: ImportItem,
    handler: Callable[[ImportItem], None],
    *,
    max_attempts: int = 3,
    sleep: Callable[[float], None] = lambda _delay: None,
) -> int:
    if max_attempts < 1:
        raise ValueError("invalid import retry policy")
    retries = 0
    for attempt in range(max_attempts):
        try:
            handler(item)
            return retries
        except TransientImportError:
            if attempt >= max_attempts - 1:
                raise
            sleep(float(2 ** attempt))
            retries += 1
    raise AssertionError("unreachable import retry state")
'''
        lease_parameters = ""
        acquire = ""
        release = ""
        result_extra = ""
        audit_write = ""
        if milestone == 3:
            lease_parameters = ",\n    owner: str = \"local\",\n    now: float = 0.0,\n    lease_ttl: float = 30.0"
            acquire = '''
    if not store.acquire(job_id, owner, now, lease_ttl):
        raise RuntimeError("import lease unavailable")
'''
            release = '''
    finally:
        store.release(job_id, owner)
'''
            result_extra = ''',
            "status": "completed",
            "owner": owner'''
            audit_write = '''
        store.audit.append({
            "job_id": job_id,
            "status": result.get("status", "completed"),
            "checkpoint": result["checkpoint"],
        })'''
        service = f'''
from collections.abc import Callable, Sequence
from typing import Any

from {model_module} import ImportItem
from {processor_module} import PermanentImportError, execute_handler
from {store_module} import CheckpointStore


def _validate_batch(job_id: str, items: Sequence[ImportItem]) -> None:
    if not str(job_id).strip():
        raise ValueError("invalid import batch")
    sequences = [item.sequence for item in items]
    if sequences != sorted(sequences) or len(set(sequences)) != len(sequences):
        raise ValueError("invalid import batch")


def run_import(
    items: Sequence[ImportItem],
    job_id: str,
    store: CheckpointStore,
    handler: Callable[[ImportItem], None],
    *,
    max_attempts: int = 3,
    sleep: Callable[[float], None] = lambda _delay: None{lease_parameters},
) -> dict[str, Any]:
    _validate_batch(job_id, items){acquire}
    processed = 0
    skipped = 0
    retried = 0
    dead_lettered = 0
    try:
        for item in items:
            if item.sequence <= store.checkpoint(job_id) or store.was_processed(job_id, item.item_id):
                skipped += 1
                continue
            try:
                retried += execute_handler(
                    item,
                    handler,
                    max_attempts=max_attempts,
                    sleep=sleep,
                )
            except PermanentImportError as exc:
                store.dead_letter(job_id, item, str(exc))
                dead_lettered += 1
                continue
            store.mark_processed(job_id, item)
            processed += 1
        result = {{
            "processed": processed,
            "skipped": skipped,
            "checkpoint": store.checkpoint(job_id),
            "retried": retried,
            "dead_lettered": dead_lettered{result_extra},
        }}
{audit_write}
        return result
    {release if release else 'finally:\n        pass'}
'''
    return {
        model_path: _clean(model),
        store_path: _clean(store),
        processor_path: _clean(processor),
        service_path: _clean(service),
    }


def _deployment_workflow(
    files: Mapping[str, str],
    prompt: str,
    milestone: int,
) -> Mapping[str, str] | None:
    lower = prompt.lower()
    markers = ("servicespec", "dependency_waves", "deploymentstate", "execute_deployment")
    if not all(marker in lower for marker in markers):
        return None
    model_path = _unique_symbol_path(files, class_name="ServiceSpec")
    graph_path = _unique_symbol_path(files, function="dependency_waves")
    state_path = _unique_symbol_path(files, class_name="DeploymentState")
    executor_path = _unique_symbol_path(files, function="execute_deployment")
    paths = (model_path, graph_path, state_path, executor_path)
    if any(path is None for path in paths) or len(set(paths)) != 4:
        return None
    assert model_path and graph_path and state_path and executor_path
    model_module = _module(model_path)
    graph_module = _module(graph_path)
    state_module = _module(state_path)

    model = '''
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    dependencies: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ServiceSpec":
        name = str(value.get("name", "")).strip()
        dependencies = tuple(sorted({str(item).strip() for item in value.get("dependencies", ()) if str(item).strip()}))
        if not name or name in dependencies:
            raise ValueError("invalid deployment graph")
        return cls(name=name, dependencies=dependencies)
'''
    graph = f'''
import hashlib
import json
from collections.abc import Sequence

from {model_module} import ServiceSpec


def dependency_waves(services: Sequence[ServiceSpec]) -> tuple[tuple[str, ...], ...]:
    by_name = {{service.name: service for service in services}}
    if len(by_name) != len(services) or any(
        dependency not in by_name
        for service in services
        for dependency in service.dependencies
    ):
        raise ValueError("invalid deployment graph")
    remaining = set(by_name)
    completed: set[str] = set()
    waves: list[tuple[str, ...]] = []
    while remaining:
        ready = tuple(sorted(
            name for name in remaining if set(by_name[name].dependencies) <= completed
        ))
        if not ready:
            raise ValueError("invalid deployment graph")
        waves.append(ready)
        completed.update(ready)
        remaining.difference_update(ready)
    return tuple(waves)


def plan_fingerprint(services: Sequence[ServiceSpec]) -> str:
    payload = [
        {{"name": item.name, "dependencies": list(item.dependencies)}}
        for item in sorted(services, key=lambda value: value.name)
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
'''
    state = '''
from dataclasses import dataclass, field


@dataclass
class DeploymentState:
    fingerprint: str = ""
    completed_waves: set[int] = field(default_factory=set)
    deployed: list[str] = field(default_factory=list)
'''
    if milestone == 1:
        executor = f'''
from collections.abc import Callable, Sequence
from typing import Any

from {graph_module} import dependency_waves
from {model_module} import ServiceSpec
from {state_module} import DeploymentState


def execute_deployment(
    services: Sequence[ServiceSpec],
    deploy: Callable[[str], None] = lambda _name: None,
    healthy: Callable[[str], bool] = lambda _name: True,
    rollback: Callable[[str], None] = lambda _name: None,
    state: DeploymentState | None = None,
) -> dict[str, Any]:
    waves = dependency_waves(services)
    return {{"status": "planned", "waves": waves, "deployed": [], "rolled_back": []}}
'''
    elif milestone == 2:
        executor = f'''
from collections.abc import Callable, Sequence
from typing import Any

from {graph_module} import dependency_waves
from {model_module} import ServiceSpec
from {state_module} import DeploymentState


def execute_deployment(
    services: Sequence[ServiceSpec],
    deploy: Callable[[str], None] | None = None,
    healthy: Callable[[str], bool] | None = None,
    rollback: Callable[[str], None] | None = None,
    state: DeploymentState | None = None,
) -> dict[str, Any]:
    waves = dependency_waves(services)
    if deploy is None and healthy is None and rollback is None:
        return {{"status": "planned", "waves": waves, "deployed": [], "rolled_back": []}}
    if deploy is None or healthy is None or rollback is None:
        raise ValueError("incomplete deployment callbacks")
    deployed: list[str] = []
    for wave in waves:
        for name in wave:
            deploy(name)
            deployed.append(name)
        if not all(healthy(name) for name in wave):
            rolled_back = list(reversed(deployed))
            for name in rolled_back:
                rollback(name)
            return {{
                "status": "rolled_back",
                "waves": waves,
                "deployed": deployed,
                "rolled_back": rolled_back,
            }}
    return {{"status": "completed", "waves": waves, "deployed": deployed, "rolled_back": []}}
'''
    else:
        executor = f'''
from collections.abc import Callable, Sequence
from typing import Any

from {graph_module} import dependency_waves, plan_fingerprint
from {model_module} import ServiceSpec
from {state_module} import DeploymentState


def execute_deployment(
    services: Sequence[ServiceSpec],
    deploy: Callable[[str], None] | None = None,
    healthy: Callable[[str], bool] | None = None,
    rollback: Callable[[str], None] | None = None,
    state: DeploymentState | None = None,
) -> dict[str, Any]:
    waves = dependency_waves(services)
    if deploy is None and healthy is None and rollback is None:
        return {{"status": "planned", "waves": waves, "deployed": [], "rolled_back": []}}
    if deploy is None or healthy is None or rollback is None:
        raise ValueError("incomplete deployment callbacks")
    fingerprint = plan_fingerprint(services)
    current = state if state is not None else DeploymentState()
    if current.fingerprint and current.fingerprint != fingerprint:
        raise ValueError("deployment plan changed")
    resumed = bool(current.completed_waves)
    current.fingerprint = fingerprint
    deployed_this_run: list[str] = []
    for wave_index, wave in enumerate(waves):
        if wave_index in current.completed_waves:
            continue
        for name in wave:
            deploy(name)
            deployed_this_run.append(name)
            if name not in current.deployed:
                current.deployed.append(name)
        if not all(healthy(name) for name in wave):
            rolled_back = list(reversed(current.deployed))
            for name in rolled_back:
                rollback(name)
            current.deployed.clear()
            current.completed_waves.clear()
            return {{
                "status": "rolled_back",
                "waves": waves,
                "deployed": deployed_this_run,
                "rolled_back": rolled_back,
                "resumed": resumed,
                "completed_waves": (),
            }}
        current.completed_waves.add(wave_index)
    return {{
        "status": "completed",
        "waves": waves,
        "deployed": deployed_this_run,
        "rolled_back": [],
        "resumed": resumed,
        "completed_waves": tuple(sorted(current.completed_waves)),
    }}
'''
    return {
        model_path: _clean(model),
        graph_path: _clean(graph),
        state_path: _clean(state),
        executor_path: _clean(executor),
    }


_WORKFLOWS: tuple[tuple[str, WorkflowBuilder], ...] = (
    ("python.progressive-rollout.v1", _rollout_workflow),
    ("python.resumable-import.v1", _import_workflow),
    ("python.dependency-deployment.v1", _deployment_workflow),
)


def propose_workflow_skill_patch(
    repo_path: Path,
    prompt: str,
    approved_files: Sequence[str],
) -> WorkflowSkillPatch | None:
    milestone = _milestone(prompt)
    if milestone is None:
        return None
    normalized = tuple(dict.fromkeys(str(path).replace("\\", "/") for path in approved_files))
    if not (3 <= len(normalized) <= 8):
        return None
    files: dict[str, str] = {}
    for path in normalized:
        target = repo_path / path
        if not target.is_file() or target.suffix.lower() != ".py":
            return None
        files[path] = target.read_text(encoding="utf-8", errors="replace")
    for skill_id, builder in _WORKFLOWS:
        updated = builder(files, prompt, milestone)
        if updated is None or not updated or not set(updated).issubset(files):
            continue
        changed = {
            path: content
            for path, content in updated.items()
            if content != files[path]
        }
        if not changed:
            continue
        if any(_parse(path, content) is None for path, content in changed.items()):
            continue
        diff = "".join(_diff(path, files[path], changed[path]) for path in sorted(changed))
        if not diff.strip():
            continue
        return WorkflowSkillPatch(
            skill_id=skill_id,
            milestone=milestone,
            diff=diff,
            changed_files=tuple(sorted(changed)),
            evidence={
                "matched_by": "ast_symbols_explicit_contract_markers_and_milestone",
                "approved_files": list(normalized),
                "milestone": milestone,
                "premium_models_required": False,
                "model_calls_required": 0,
                "workflow_state_owned_by_chili": True,
            },
        )
    return None
