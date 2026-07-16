from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.models import ProjectAutonomyRun  # noqa: E402
from app.models.code_brain import CodeRepo  # noqa: E402
from app.services.project_autonomy import orchestrator  # noqa: E402
from scripts import autopilot_meso_project_workflow_tournament as meso  # noqa: E402
from scripts import autopilot_offline_project_autonomy_benchmark as offline_benchmark  # noqa: E402


SCHEMA = "chili.macro-long-horizon-tournament.v1"
DEFAULT_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "MACRO_LONG_HORIZON_TOURNAMENT_BENCHMARK.md"
DEFAULT_ARTIFACT_ROOT = (
    REPO_ROOT / "project_ws" / "AgentOps" / "system_level_tournaments" / "macro"
)
SOURCE_KINDS = meso.SOURCE_KINDS
MODEL_NAMES = meso.MODEL_NAMES
MAX_PHASE_ATTEMPTS = 2


@dataclasses.dataclass(frozen=True)
class WorkflowPhase:
    phase_id: str
    title: str
    goal: str
    visible_tests: Mapping[str, str]
    hidden_tests: Mapping[str, str]
    required_files: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class MacroTask:
    task_id: str
    title: str
    source_files: Mapping[str, str]
    phases: tuple[WorkflowPhase, ...]

    @property
    def allowed_files(self) -> tuple[str, ...]:
        return tuple(sorted(self.source_files))


@dataclasses.dataclass(frozen=True)
class MacroContestantResult:
    task_id: str
    source_kind: str
    model_name: str
    duration_seconds: float
    attempts: int
    quality_score: int
    behavior_passed: bool
    phases_passed: int
    phase_count: int
    scope_valid: bool
    tests_unchanged: bool
    required_files_changed: bool
    continuity_valid: bool
    semantic_review_passed: bool
    premium_calls: int | None
    changed_files: tuple[str, ...]
    validation_output: str
    failure: str
    final_diff: str
    phase_results: tuple[Mapping[str, Any], ...] = ()
    model_events: tuple[Mapping[str, Any], ...] = ()

    @property
    def eligible(self) -> bool:
        return (
            self.behavior_passed
            and self.scope_valid
            and self.tests_unchanged
            and self.required_files_changed
            and self.continuity_valid
            and self.semantic_review_passed
        )


FrontierCall = meso.FrontierCall
Progress = Callable[[str], None]


def _clean(value: str) -> str:
    import textwrap

    return textwrap.dedent(value).lstrip("\n")


def default_tasks() -> tuple[MacroTask, ...]:
    rollout_files = {
        "app/config.py": _clean(
            '''
            from dataclasses import dataclass
            from typing import Mapping


            @dataclass(frozen=True)
            class RolloutConfig:
                enabled: bool = False
                percentage: int = 0
                salt: str = ""

                @classmethod
                def from_mapping(cls, values: Mapping[str, str]) -> "RolloutConfig":
                    return cls(bool(values.get("ENABLED")), int(values.get("PERCENTAGE", "0")), values.get("SALT", ""))
            '''
        ),
        "app/cohort.py": _clean(
            '''
            def stable_bucket(subject_id: str, salt: str) -> int:
                return hash((subject_id, salt)) % 100
            '''
        ),
        "app/decision.py": _clean(
            '''
            from typing import Any

            from app.config import RolloutConfig


            def decide_rollout(subject_id: str, config: RolloutConfig) -> dict[str, Any]:
                return {"enabled": config.enabled}
            '''
        ),
        "app/service.py": _clean(
            '''
            from typing import Any, Mapping


            def evaluate_request(subject_id: str, raw_config: Mapping[str, str]) -> dict[str, Any]:
                return {"enabled": False}
            '''
        ),
    }
    rollout_phases = (
        WorkflowPhase(
            phase_id="milestone-1",
            title="Deterministic cohort foundation",
            goal=(
                "Milestone 1/3. Repair the progressive rollout workflow across app/config.py, "
                "app/cohort.py, app/decision.py, and app/service.py. RolloutConfig.from_mapping "
                "must parse explicit boolean values, percentage 0..100, and a non-empty salt, "
                "raising ValueError('invalid rollout config') for invalid input. stable_bucket "
                "must use SHA-256 over salt and subject_id, reject blanks, and return 0..99. "
                "decide_rollout must expose enabled, reason, and bucket; evaluate_request must "
                "compose the contract. Preserve tests and public names RolloutConfig, "
                "stable_bucket, decide_rollout, and evaluate_request."
            ),
            visible_tests={
                "tests/test_rollout_contract_m1.py": _clean(
                    '''
                    from app.cohort import stable_bucket
                    from app.service import evaluate_request


                    def test_rollout_is_stable_and_one_hundred_percent_enables():
                        raw = {"ENABLED": "true", "PERCENTAGE": "100", "SALT": "pepper"}
                        first = evaluate_request("acct-1", raw)
                        second = evaluate_request("acct-1", raw)
                        assert first == second
                        assert first["enabled"] is True
                        assert first["reason"] == "cohort"
                        assert first["bucket"] == stable_bucket("acct-1", "pepper")


                    def test_disabled_rollout_has_no_bucket():
                        result = evaluate_request("acct-1", {"ENABLED": "false", "PERCENTAGE": "100", "SALT": "x"})
                        assert result["enabled"] is False
                        assert result["reason"] == "disabled"
                        assert result["bucket"] is None
                    '''
                )
            },
            hidden_tests={
                "hidden_tests/test_rollout_m1_edges.py": _clean(
                    '''
                    import hashlib
                    import pytest

                    from app.cohort import stable_bucket
                    from app.config import RolloutConfig
                    from app.service import evaluate_request


                    @pytest.mark.parametrize("percentage", ["-1", "101", "x"])
                    def test_invalid_percentage_fails_closed(percentage):
                        with pytest.raises(ValueError, match="invalid rollout config"):
                            RolloutConfig.from_mapping({"ENABLED": "true", "PERCENTAGE": percentage, "SALT": "x"})


                    def test_bucket_matches_portable_sha256_contract():
                        expected = int.from_bytes(hashlib.sha256(b"pepper:acct-2").digest()[:8], "big") % 100
                        assert stable_bucket("acct-2", "pepper") == expected


                    def test_zero_percent_disables_subject():
                        result = evaluate_request("acct", {"ENABLED": "true", "PERCENTAGE": "0", "SALT": "x"})
                        assert result["enabled"] is False
                        assert result["reason"] == "outside_cohort"
                    '''
                )
            },
            required_files=tuple(sorted(rollout_files)),
        ),
        WorkflowPhase(
            phase_id="milestone-2",
            title="Precedence and safe audit",
            goal=(
                "Milestone 2/3. Evolve the existing workflow across app/config.py, app/cohort.py, "
                "app/decision.py, and app/service.py without regressing milestone 1. Add trimmed "
                "comma-separated allowlist and denylist fields to RolloutConfig; overlap is invalid. "
                "Decision precedence is denylist, global disabled, allowlist, then cohort. "
                "evaluate_request accepts an optional audit list and appends a non-secret decision "
                "record that never exposes salt. Preserve RolloutConfig, stable_bucket, "
                "decide_rollout, evaluate_request, and every prior test."
            ),
            visible_tests={
                "tests/test_rollout_contract_m2.py": _clean(
                    '''
                    from app.service import evaluate_request


                    def test_lists_have_explicit_precedence_and_audit_is_safe():
                        audit = []
                        raw = {
                            "ENABLED": "true",
                            "PERCENTAGE": "0",
                            "SALT": "secret-salt",
                            "ALLOWLIST": " allow ",
                            "DENYLIST": "deny",
                        }
                        assert evaluate_request("allow", raw, audit)["reason"] == "allowlist"
                        assert evaluate_request("deny", raw, audit)["reason"] == "denylist"
                        assert "secret-salt" not in repr(audit)
                    '''
                )
            },
            hidden_tests={
                "hidden_tests/test_rollout_m2_edges.py": _clean(
                    '''
                    import pytest

                    from app.config import RolloutConfig
                    from app.service import evaluate_request


                    def test_overlapping_lists_are_rejected():
                        with pytest.raises(ValueError, match="invalid rollout config"):
                            RolloutConfig.from_mapping({"SALT": "x", "ALLOWLIST": "same", "DENYLIST": "same"})


                    def test_global_disable_precedes_allowlist():
                        result = evaluate_request("vip", {"ENABLED": "false", "SALT": "x", "ALLOWLIST": "vip"})
                        assert result["reason"] == "disabled"
                    '''
                )
            },
            required_files=("app/config.py", "app/decision.py", "app/service.py"),
        ),
        WorkflowPhase(
            phase_id="milestone-3",
            title="Kill switch and versioned evidence",
            goal=(
                "Milestone 3/3. Finish the workflow across app/config.py, app/cohort.py, "
                "app/decision.py, and app/service.py while preserving milestones 1 and 2. Add a "
                "kill switch with highest precedence and positive config_version. Every decision "
                "must include config_version and a deterministic decision_id derived without the "
                "salt. Add summarize_decisions for reason counts; audit data must remain secret-free. "
                "Preserve RolloutConfig, stable_bucket, decide_rollout, evaluate_request, and all tests."
            ),
            visible_tests={
                "tests/test_rollout_contract_m3.py": _clean(
                    '''
                    from app.service import evaluate_request, summarize_decisions


                    def test_kill_switch_and_versioned_decision_evidence():
                        audit = []
                        raw = {
                            "ENABLED": "true",
                            "PERCENTAGE": "100",
                            "SALT": "do-not-log",
                            "KILL_SWITCH": "true",
                            "CONFIG_VERSION": "7",
                        }
                        first = evaluate_request("acct", raw, audit)
                        second = evaluate_request("acct", raw)
                        assert first == second
                        assert first["reason"] == "kill_switch"
                        assert first["config_version"] == 7
                        assert len(first["decision_id"]) == 16
                        assert summarize_decisions(audit) == {"kill_switch": 1}
                    '''
                )
            },
            hidden_tests={
                "hidden_tests/test_rollout_m3_edges.py": _clean(
                    '''
                    import pytest

                    from app.config import RolloutConfig
                    from app.service import evaluate_request


                    def test_bad_config_version_is_rejected():
                        with pytest.raises(ValueError, match="invalid rollout config"):
                            RolloutConfig.from_mapping({"SALT": "x", "CONFIG_VERSION": "0"})


                    def test_decision_and_audit_never_contain_salt():
                        audit = []
                        result = evaluate_request("acct", {"ENABLED": "true", "PERCENTAGE": "100", "SALT": "private"}, audit)
                        assert "private" not in repr(result)
                        assert "private" not in repr(audit)
                    '''
                )
            },
            required_files=("app/config.py", "app/decision.py", "app/service.py"),
        ),
    )

    import_files = {
        "app/model.py": _clean(
            '''
            from dataclasses import dataclass
            from typing import Any, Mapping


            @dataclass(frozen=True)
            class ImportItem:
                item_id: str
                sequence: int
                payload: Mapping[str, Any]
            '''
        ),
        "app/store.py": _clean(
            '''
            class CheckpointStore:
                def __init__(self) -> None:
                    self.value = 0
            '''
        ),
        "app/processor.py": _clean(
            '''
            def execute_handler(item, handler):
                return handler(item)
            '''
        ),
        "app/service.py": _clean(
            '''
            def run_import(items, job_id, store, handler):
                for item in items:
                    handler(item)
                return {"processed": len(items)}
            '''
        ),
    }
    import_phases = (
        WorkflowPhase(
            phase_id="milestone-1",
            title="Idempotent checkpoint foundation",
            goal=(
                "Milestone 1/3. Repair the resumable import workflow across app/model.py, "
                "app/store.py, app/processor.py, and app/service.py. ImportItem validates nonblank "
                "item_id, positive sequence, and mapping payload. CheckpointStore tracks per-job "
                "monotonic checkpoints and processed ids. execute_handler invokes the handler. "
                "run_import validates strictly increasing unique sequences, processes in order, "
                "skips already completed items, checkpoints only after success, and returns "
                "processed, skipped, and checkpoint. Preserve ImportItem, CheckpointStore, "
                "execute_handler, run_import, and tests."
            ),
            visible_tests={
                "tests/test_import_contract_m1.py": _clean(
                    '''
                    from app.model import ImportItem
                    from app.service import run_import
                    from app.store import CheckpointStore


                    def test_import_is_idempotent_and_checkpointed():
                        items = [ImportItem("a", 1, {}), ImportItem("b", 2, {})]
                        store = CheckpointStore()
                        seen = []
                        first = run_import(items, "job", store, lambda item: seen.append(item.item_id))
                        second = run_import(items, "job", store, lambda item: seen.append(item.item_id))
                        assert {key: first[key] for key in ("processed", "skipped", "checkpoint")} == {
                            "processed": 2, "skipped": 0, "checkpoint": 2
                        }
                        assert {key: second[key] for key in ("processed", "skipped", "checkpoint")} == {
                            "processed": 0, "skipped": 2, "checkpoint": 2
                        }
                        assert seen == ["a", "b"]
                    '''
                )
            },
            hidden_tests={
                "hidden_tests/test_import_m1_edges.py": _clean(
                    '''
                    import pytest

                    from app.model import ImportItem
                    from app.service import run_import
                    from app.store import CheckpointStore


                    def test_checkpoint_advances_only_after_success():
                        store = CheckpointStore()
                        items = [ImportItem("a", 1, {}), ImportItem("b", 2, {})]
                        with pytest.raises(RuntimeError, match="stop"):
                            run_import(items, "job", store, lambda item: (_ for _ in ()).throw(RuntimeError("stop")) if item.item_id == "b" else None)
                        assert store.checkpoint("job") == 1


                    def test_out_of_order_batch_is_rejected():
                        with pytest.raises(ValueError, match="invalid import batch"):
                            run_import([ImportItem("b", 2, {}), ImportItem("a", 1, {})], "job", CheckpointStore(), lambda item: None)
                    '''
                )
            },
            required_files=tuple(sorted(import_files)),
        ),
        WorkflowPhase(
            phase_id="milestone-2",
            title="Bounded retry and dead letter",
            goal=(
                "Milestone 2/3. Evolve the import workflow across app/model.py, app/store.py, "
                "app/processor.py, and app/service.py without regressing milestone 1. Add "
                "TransientImportError and PermanentImportError. execute_handler retries only "
                "transient failures up to max_attempts with exponential 1,2,... sleep and returns "
                "retry count. Permanent failures are dead-lettered without payload and advance "
                "the checkpoint; exhausted transient failure is re-raised without advancement. "
                "run_import returns retried and dead_lettered. Preserve ImportItem, CheckpointStore, "
                "execute_handler, run_import, and prior tests."
            ),
            visible_tests={
                "tests/test_import_contract_m2.py": _clean(
                    '''
                    from app.model import ImportItem
                    from app.processor import PermanentImportError, TransientImportError
                    from app.service import run_import
                    from app.store import CheckpointStore


                    def test_retry_and_dead_letter_are_bounded():
                        store = CheckpointStore()
                        attempts = {"a": 0}
                        sleeps = []

                        def handler(item):
                            if item.item_id == "a":
                                attempts["a"] += 1
                                if attempts["a"] == 1:
                                    raise TransientImportError("later")
                            if item.item_id == "b":
                                raise PermanentImportError("bad row")

                        result = run_import(
                            [ImportItem("a", 1, {}), ImportItem("b", 2, {"secret": 1})],
                            "job", store, handler, sleep=sleeps.append
                        )
                        assert {key: result[key] for key in ("processed", "skipped", "checkpoint", "retried", "dead_lettered")} == {
                            "processed": 1, "skipped": 0, "checkpoint": 2, "retried": 1, "dead_lettered": 1
                        }
                        assert sleeps == [1.0]
                        assert store.dead_letters == [{"job_id": "job", "item_id": "b", "reason": "bad row"}]
                    '''
                )
            },
            hidden_tests={
                "hidden_tests/test_import_m2_edges.py": _clean(
                    '''
                    import pytest

                    from app.model import ImportItem
                    from app.processor import TransientImportError
                    from app.service import run_import
                    from app.store import CheckpointStore


                    def test_exhausted_transient_does_not_checkpoint():
                        store = CheckpointStore()
                        with pytest.raises(TransientImportError):
                            run_import([ImportItem("a", 1, {})], "job", store, lambda item: (_ for _ in ()).throw(TransientImportError("down")), max_attempts=2)
                        assert store.checkpoint("job") == 0
                    '''
                )
            },
            required_files=("app/store.py", "app/processor.py", "app/service.py"),
        ),
        WorkflowPhase(
            phase_id="milestone-3",
            title="Lease-safe resumability",
            goal=(
                "Milestone 3/3. Finish the import workflow across app/model.py, app/store.py, "
                "app/processor.py, and app/service.py while preserving milestones 1 and 2. "
                "CheckpointStore must provide owner leases with expiry, stale takeover, and "
                "owner-only release. run_import accepts owner, now, and positive lease_ttl, fails "
                "with RuntimeError('import lease unavailable') on an active foreign lease, always "
                "releases its own lease, and appends payload-free audit summaries. Preserve "
                "ImportItem, CheckpointStore, execute_handler, run_import, and all tests."
            ),
            visible_tests={
                "tests/test_import_contract_m3.py": _clean(
                    '''
                    import pytest

                    from app.model import ImportItem
                    from app.service import run_import
                    from app.store import CheckpointStore


                    def test_active_foreign_lease_blocks_and_stale_lease_is_taken_over():
                        store = CheckpointStore()
                        assert store.acquire("job", "old", 0.0, 10.0) is True
                        with pytest.raises(RuntimeError, match="import lease unavailable"):
                            run_import([], "job", store, lambda item: None, owner="new", now=5.0)
                        result = run_import([ImportItem("a", 1, {})], "job", store, lambda item: None, owner="new", now=11.0)
                        assert result["status"] == "completed"
                        assert result["owner"] == "new"
                        assert "job" not in store.leases
                    '''
                )
            },
            hidden_tests={
                "hidden_tests/test_import_m3_edges.py": _clean(
                    '''
                    from app.model import ImportItem
                    from app.service import run_import
                    from app.store import CheckpointStore


                    def test_audit_is_summary_only_and_resume_skips_checkpointed_rows():
                        store = CheckpointStore()
                        item = ImportItem("a", 1, {"password": "secret"})
                        run_import([item], "job", store, lambda value: None, owner="one", now=0.0)
                        run_import([item], "job", store, lambda value: None, owner="two", now=1.0)
                        assert "secret" not in repr(store.audit)
                        assert store.audit[-1]["checkpoint"] == 1
                    '''
                )
            },
            required_files=("app/store.py", "app/service.py"),
        ),
    )

    deployment_files = {
        "app/model.py": _clean(
            '''
            from dataclasses import dataclass


            @dataclass(frozen=True)
            class ServiceSpec:
                name: str
                dependencies: tuple[str, ...] = ()
            '''
        ),
        "app/graph.py": _clean(
            '''
            def dependency_waves(services):
                return tuple((service.name,) for service in services)
            '''
        ),
        "app/state.py": _clean(
            '''
            class DeploymentState:
                pass
            '''
        ),
        "app/executor.py": _clean(
            '''
            def execute_deployment(services, deploy=lambda name: None, healthy=lambda name: True, rollback=lambda name: None, state=None):
                return {"status": "planned"}
            '''
        ),
    }
    deployment_phases = (
        WorkflowPhase(
            phase_id="milestone-1",
            title="Deterministic dependency plan",
            goal=(
                "Milestone 1/3. Repair the deployment workflow across app/model.py, app/graph.py, "
                "app/state.py, and app/executor.py. ServiceSpec validates names and dependencies. "
                "dependency_waves must reject duplicate names, missing dependencies, self edges, "
                "and cycles with ValueError('invalid deployment graph'), and return deterministic "
                "sorted topological waves. execute_deployment returns a planned envelope. Preserve "
                "ServiceSpec, dependency_waves, DeploymentState, execute_deployment, and tests."
            ),
            visible_tests={
                "tests/test_deploy_contract_m1.py": _clean(
                    '''
                    from app.executor import execute_deployment
                    from app.graph import dependency_waves
                    from app.model import ServiceSpec


                    def test_dependency_waves_are_deterministic():
                        services = [
                            ServiceSpec("web", ("api", "worker")),
                            ServiceSpec("worker", ("db",)),
                            ServiceSpec("api", ("db",)),
                            ServiceSpec("db"),
                        ]
                        assert dependency_waves(services) == (("db",), ("api", "worker"), ("web",))
                        assert execute_deployment(services)["status"] == "planned"
                    '''
                )
            },
            hidden_tests={
                "hidden_tests/test_deploy_m1_edges.py": _clean(
                    '''
                    import pytest

                    from app.graph import dependency_waves
                    from app.model import ServiceSpec


                    @pytest.mark.parametrize("services", [
                        [ServiceSpec("a"), ServiceSpec("a")],
                        [ServiceSpec("a", ("missing",))],
                        [ServiceSpec("a", ("b",)), ServiceSpec("b", ("a",))],
                    ])
                    def test_invalid_graphs_fail_closed(services):
                        with pytest.raises(ValueError, match="invalid deployment graph"):
                            dependency_waves(services)
                    '''
                )
            },
            required_files=tuple(sorted(deployment_files)),
        ),
        WorkflowPhase(
            phase_id="milestone-2",
            title="Health-gated rollback",
            goal=(
                "Milestone 2/3. Evolve the deployment workflow across app/model.py, app/graph.py, "
                "app/state.py, and app/executor.py without regressing milestone 1. "
                "execute_deployment must deploy each wave in deterministic order, health-check "
                "the whole wave, and on any unhealthy service rollback every deployed service in "
                "strict reverse order. Return status, waves, deployed, and rolled_back. Preserve "
                "ServiceSpec, dependency_waves, DeploymentState, execute_deployment, and prior tests."
            ),
            visible_tests={
                "tests/test_deploy_contract_m2.py": _clean(
                    '''
                    from app.executor import execute_deployment
                    from app.model import ServiceSpec


                    def test_unhealthy_wave_rolls_back_in_reverse_order():
                        actions = []
                        result = execute_deployment(
                            [ServiceSpec("api", ("db",)), ServiceSpec("db")],
                            lambda name: actions.append(("deploy", name)),
                            lambda name: name != "api",
                            lambda name: actions.append(("rollback", name)),
                        )
                        assert result["status"] == "rolled_back"
                        assert result["rolled_back"] == ["api", "db"]
                        assert actions[-2:] == [("rollback", "api"), ("rollback", "db")]
                    '''
                )
            },
            hidden_tests={
                "hidden_tests/test_deploy_m2_edges.py": _clean(
                    '''
                    from app.executor import execute_deployment
                    from app.model import ServiceSpec


                    def test_healthy_graph_completes_without_rollback():
                        rolled = []
                        result = execute_deployment([ServiceSpec("db")], lambda name: None, lambda name: True, rolled.append)
                        assert result["status"] == "completed"
                        assert result["rolled_back"] == []
                        assert rolled == []
                    '''
                )
            },
            required_files=("app/executor.py",),
        ),
        WorkflowPhase(
            phase_id="milestone-3",
            title="Fingerprint-safe resume",
            goal=(
                "Milestone 3/3. Finish the deployment workflow across app/model.py, app/graph.py, "
                "app/state.py, and app/executor.py while preserving milestones 1 and 2. Add a "
                "deterministic plan_fingerprint and durable DeploymentState fingerprint, completed "
                "waves, and deployed services. execute_deployment must skip completed waves on "
                "resume, expose resumed and completed_waves, and fail closed with "
                "ValueError('deployment plan changed') when state belongs to another graph. "
                "Rollback includes prior deployed state. Preserve ServiceSpec, dependency_waves, "
                "DeploymentState, execute_deployment, all public names, and tests."
            ),
            visible_tests={
                "tests/test_deploy_contract_m3.py": _clean(
                    '''
                    import pytest

                    from app.executor import execute_deployment
                    from app.graph import plan_fingerprint
                    from app.model import ServiceSpec
                    from app.state import DeploymentState


                    def test_resume_skips_completed_wave_and_checks_fingerprint():
                        services = [ServiceSpec("api", ("db",)), ServiceSpec("db")]
                        state = DeploymentState(
                            fingerprint=plan_fingerprint(services),
                            completed_waves={0},
                            deployed=["db"],
                        )
                        deployed = []
                        result = execute_deployment(services, deployed.append, lambda name: True, lambda name: None, state)
                        assert deployed == ["api"]
                        assert result["resumed"] is True
                        assert result["completed_waves"] == (0, 1)
                        with pytest.raises(ValueError, match="deployment plan changed"):
                            execute_deployment([ServiceSpec("other")], deployed.append, lambda name: True, lambda name: None, state)
                    '''
                )
            },
            hidden_tests={
                "hidden_tests/test_deploy_m3_edges.py": _clean(
                    '''
                    from app.executor import execute_deployment
                    from app.graph import plan_fingerprint
                    from app.model import ServiceSpec
                    from app.state import DeploymentState


                    def test_resume_failure_rolls_back_prior_and_new_services():
                        services = [ServiceSpec("api", ("db",)), ServiceSpec("db")]
                        state = DeploymentState(plan_fingerprint(services), {0}, ["db"])
                        rolled = []
                        result = execute_deployment(services, lambda name: None, lambda name: False, rolled.append, state)
                        assert result["rolled_back"] == ["api", "db"]
                        assert rolled == ["api", "db"]
                        assert state.completed_waves == set()
                    '''
                )
            },
            required_files=("app/executor.py",),
        ),
    )
    return (
        MacroTask("progressive-rollout", "Progressive rollout evolution", rollout_files, rollout_phases),
        MacroTask("resumable-import", "Resumable import evolution", import_files, import_phases),
        MacroTask("dependency-deployment", "Dependency deployment evolution", deployment_files, deployment_phases),
    )


def _write_files(root: Path, files: Mapping[str, str]) -> None:
    meso._write_files(root, files)


def _init_task_repo(task: MacroTask, root: Path) -> None:
    _write_files(root, task.source_files)
    for package in {Path(path).parent for path in task.source_files if "/" in path}:
        (root / package / "__init__.py").write_text("", encoding="utf-8")
    completed = orchestrator._git(root, ["init"], timeout=60)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout or "git init failed")
    orchestrator._git(root, ["config", "user.name", "CHILI Macro Tournament"], timeout=30)
    orchestrator._git(root, ["config", "user.email", "macro@localhost"], timeout=30)
    orchestrator._git(root, ["add", "."], timeout=30)
    committed = orchestrator._git(root, ["commit", "-m", "fixture"], timeout=60)
    if committed.returncode != 0:
        raise RuntimeError(committed.stderr or committed.stdout or "fixture commit failed")


def _visible_tests(task: MacroTask, phase_index: int) -> dict[str, str]:
    return {
        path: content
        for phase in task.phases[: phase_index + 1]
        for path, content in phase.visible_tests.items()
    }


def _hidden_tests(task: MacroTask, phase_index: int) -> dict[str, str]:
    return {
        path: content
        for phase in task.phases[: phase_index + 1]
        for path, content in phase.hidden_tests.items()
    }


def _prepare_phase(root: Path, task: MacroTask, phase_index: int) -> dict[str, str]:
    visible = _visible_tests(task, phase_index)
    _write_files(root, task.phases[phase_index].visible_tests)
    orchestrator._git(root, ["add", "."], timeout=30)
    committed = orchestrator._git(
        root,
        ["commit", "--allow-empty", "-m", f"phase {phase_index + 1} baseline"],
        timeout=60,
    )
    if committed.returncode != 0:
        raise RuntimeError(committed.stderr or committed.stdout or "phase baseline commit failed")
    return {path: meso.hashlib.sha256((root / path).read_bytes()).hexdigest() for path in visible}


def _phase_snapshot(root: Path, task: MacroTask, phase_index: int) -> str:
    sections = []
    paths = sorted((*task.source_files, *_visible_tests(task, phase_index)))
    for path in paths:
        sections.extend(
            [
                f"### {path}",
                "```python",
                (root / path).read_text(encoding="utf-8", errors="replace").rstrip(),
                "```",
                "",
            ]
        )
    return "\n".join(sections).rstrip()


def render_frontier_prompt(
    task: MacroTask,
    phase_index: int,
    root: Path,
    *,
    validation_failure: str = "",
    previous_response: str = "",
) -> str:
    phase = task.phases[phase_index]
    completed = [item.phase_id for item in task.phases[:phase_index]]
    parts = [
        "You are competing in a measured long-horizon repository evolution tournament.",
        "Return one applicable unified diff only, preferably in a ```diff fence.",
        "Do not edit tests, add dependencies, use placeholders, or touch files outside the approved source files.",
        "Preserve all behavior and tests from prior milestones; this repository is the durable workflow state.",
        "Held-out tests are not shown and will be cumulative.",
        "",
        f"Project: {task.title}",
        f"Current milestone: {phase.phase_id}",
        f"Completed milestones: {', '.join(completed) if completed else 'none'}",
        f"Goal: {phase.goal}",
        f"Approved source files: {', '.join(task.allowed_files)}",
        "",
        "Current repository snapshot:",
        _phase_snapshot(root, task, phase_index),
    ]
    if validation_failure:
        parts.extend(
            [
                "",
                "The previous attempt failed cumulative validation. Repair the current state using this evidence:",
                validation_failure[-6000:],
                "",
                "Previous response:",
                previous_response[-5000:],
            ]
        )
    return "\n".join(parts).strip() + "\n"


def _run_phase_validation(root: Path, task: MacroTask, phase_index: int) -> tuple[bool, str]:
    hidden_root = (root / "hidden_tests").resolve()
    if not str(hidden_root).startswith(str(root.resolve())):
        raise RuntimeError("hidden test path escaped tournament repository")
    if hidden_root.exists():
        shutil.rmtree(hidden_root)
    _write_files(root, _hidden_tests(task, phase_index))
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "hidden_tests", "-q"],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    output = "\n".join(
        line.rstrip()
        for line in (completed.stdout + "\n" + completed.stderr).splitlines()
        if line.strip()
    )
    shutil.rmtree(hidden_root)
    return completed.returncode == 0, output[-6000:]


def _phase_diff(root: Path, task: MacroTask) -> str:
    completed = orchestrator._git(root, ["diff", "--", *task.allowed_files], timeout=60)
    return completed.stdout or ""


def _commit_phase(root: Path, phase_id: str) -> None:
    orchestrator._git(root, ["add", *[str(path) for path in sorted(root.glob("app/*.py"))]], timeout=30)
    completed = orchestrator._git(root, ["commit", "-m", phase_id], timeout=60)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout or "phase commit failed")


def _semantic_review(task: MacroTask, phase: WorkflowPhase, changed: Sequence[str], diff: str) -> bool:
    plan = {
        "analysis": f"Continue {task.title} through {phase.phase_id} without regressing prior milestones.",
        "files": [{"path": path, "action": "modify", "description": phase.goal} for path in phase.required_files],
        "success_criteria": [phase.goal, "All cumulative visible and held-out tests pass."],
        "validation_targets": ["cumulative visible and held-out pytest behavior"],
        "risks": ["long-horizon contract regression", "unsafe state resume"],
    }
    review = orchestrator.semantic_patch_review_gate(
        plan,
        list(changed),
        diff_text=diff,
        validation=({"status": "passed", "test_files": sorted(phase.visible_tests)},),
    )
    return bool(review.get("passed"))


def quality_score(
    *,
    behavior_passed: bool,
    scope_valid: bool,
    tests_unchanged: bool,
    required_files_changed: bool,
    continuity_valid: bool,
    semantic_review_passed: bool,
) -> int:
    return (
        (60 if behavior_passed else 0)
        + (10 if scope_valid else 0)
        + (10 if tests_unchanged else 0)
        + (5 if required_files_changed else 0)
        + (10 if continuity_valid else 0)
        + (5 if semantic_review_passed else 0)
    )


def _build_result(
    task: MacroTask,
    source_kind: str,
    *,
    duration: float,
    attempts: int,
    phase_results: Sequence[Mapping[str, Any]],
    premium_calls: int | None,
    failure: str,
    model_events: Sequence[Mapping[str, Any]] = (),
) -> MacroContestantResult:
    all_diffs = [str(item.get("diff") or "") for item in phase_results]
    final_diff = "\n".join(value.rstrip() for value in all_diffs if value.strip()) + ("\n" if all_diffs else "")
    changed_by_phase = [set(meso._changed_files(value)) for value in all_diffs]
    changed = tuple(sorted(set().union(*changed_by_phase) if changed_by_phase else set()))
    scope_valid = bool(changed) and set(changed).issubset(task.allowed_files)
    tests_unchanged = all(bool(item.get("tests_unchanged")) for item in phase_results)
    # A correct long-horizon phase may legitimately leave an already-correct contract
    # participant untouched. Require scoped implementation evidence for every phase,
    # but never reward unnecessary edits.
    required = (
        len(phase_results) == len(task.phases)
        and all(bool(changed_by_phase[index]) for index in range(len(phase_results)))
    )
    semantic = all(bool(item.get("semantic_review_passed")) for item in phase_results)
    phases_passed = sum(1 for item in phase_results if item.get("passed"))
    behavior = phases_passed == len(task.phases)
    continuity = behavior and all(int(item.get("cumulative_phase_count") or 0) == index + 1 for index, item in enumerate(phase_results))
    score = quality_score(
        behavior_passed=behavior,
        scope_valid=scope_valid,
        tests_unchanged=tests_unchanged,
        required_files_changed=required,
        continuity_valid=continuity,
        semantic_review_passed=semantic,
    )
    validation_output = str(phase_results[-1].get("validation_output") if phase_results else "validation not run")
    return MacroContestantResult(
        task_id=task.task_id,
        source_kind=source_kind,
        model_name=MODEL_NAMES[source_kind],
        duration_seconds=round(duration, 3),
        attempts=attempts,
        quality_score=score,
        behavior_passed=behavior,
        phases_passed=phases_passed,
        phase_count=len(task.phases),
        scope_valid=scope_valid,
        tests_unchanged=tests_unchanged,
        required_files_changed=required,
        continuity_valid=continuity,
        semantic_review_passed=semantic,
        premium_calls=premium_calls,
        changed_files=changed,
        validation_output=validation_output[-3000:],
        failure=failure[:2400],
        final_diff=final_diff,
        phase_results=tuple(dict(item) for item in phase_results),
        model_events=tuple(dict(item) for item in model_events),
    )


def run_frontier_contestant(
    task: MacroTask,
    source_kind: str,
    root: Path,
    *,
    call: FrontierCall = meso._default_frontier_call,
    timeout_seconds: int = 900,
    max_budget_usd: float = 10.0,
    artifact_dir: Path | None = None,
    progress: Progress | None = None,
) -> MacroContestantResult:
    duration = 0.0
    attempts = 0
    failure = ""
    phase_results: list[dict[str, Any]] = []
    for phase_index, phase in enumerate(task.phases):
        hashes = _prepare_phase(root, task, phase_index)
        previous_response = ""
        passed = False
        validation_output = "validation not run"
        phase_diff = ""
        phase_attempts = 0
        for attempt in range(1, MAX_PHASE_ATTEMPTS + 1):
            attempts += 1
            phase_attempts = attempt
            prompt = render_frontier_prompt(
                task,
                phase_index,
                root,
                validation_failure=failure,
                previous_response=previous_response,
            )
            phase_dir = artifact_dir / phase.phase_id if artifact_dir else None
            if phase_dir:
                phase_dir.mkdir(parents=True, exist_ok=True)
                (phase_dir / f"prompt_attempt_{attempt}.txt").write_text(prompt, encoding="utf-8")
            try:
                response, elapsed, command = call(source_kind, prompt, timeout_seconds, max_budget_usd)
                duration += elapsed
            except Exception as exc:
                failure = f"RuntimeError: {exc}" if not isinstance(exc, RuntimeError) else f"RuntimeError: {exc}"
                break
            previous_response = response
            if phase_dir:
                (phase_dir / f"response_attempt_{attempt}.txt").write_text(response, encoding="utf-8")
                (phase_dir / f"command_attempt_{attempt}.txt").write_text(command + "\n", encoding="utf-8")
            applied, evidence, patch = meso._apply_scoped_patch(root, task, response)
            if phase_dir and patch:
                (phase_dir / f"patch_attempt_{attempt}.diff").write_text(patch, encoding="utf-8")
            if not applied:
                failure = "patch rejected: " + evidence
                continue
            passed, validation_output = _run_phase_validation(root, task, phase_index)
            if passed:
                failure = ""
                break
            failure = "validation failed: " + validation_output
        if not passed:
            phase_results.append(
                {
                    "phase_id": phase.phase_id,
                    "passed": False,
                    "attempts": phase_attempts,
                    "tests_unchanged": hashes == meso._hash_files(root, tuple(hashes)),
                    "semantic_review_passed": False,
                    "cumulative_phase_count": phase_index + 1,
                    "validation_output": validation_output,
                    "diff": _phase_diff(root, task),
                }
            )
            break
        phase_diff = _phase_diff(root, task)
        changed = meso._changed_files(phase_diff)
        semantic = _semantic_review(task, phase, changed, phase_diff)
        phase_results.append(
            {
                "phase_id": phase.phase_id,
                "passed": True,
                "attempts": phase_attempts,
                "tests_unchanged": hashes == meso._hash_files(root, tuple(hashes)),
                "semantic_review_passed": semantic,
                "cumulative_phase_count": phase_index + 1,
                "validation_output": validation_output,
                "changed_files": list(changed),
                "diff": phase_diff,
            }
        )
        _commit_phase(root, phase.phase_id)
        if progress:
            progress(f"{task.task_id}/{source_kind}/{phase.phase_id} passed")
    return _build_result(
        task,
        source_kind,
        duration=duration,
        attempts=attempts,
        phase_results=phase_results,
        premium_calls=None,
        failure=failure,
    )


def run_chili_contestant(
    task: MacroTask,
    root: Path,
    *,
    artifact_dir: Path | None = None,
    progress: Progress | None = None,
) -> MacroContestantResult:
    premium_calls = 0
    model_events: list[dict[str, Any]] = []

    def forbidden_premium_call(*_args, **_kwargs):
        nonlocal premium_calls
        premium_calls += 1
        raise AssertionError("premium model route called inside CHILI macro contestant")

    saved_env = {key: os.environ.get(key) for key in offline_benchmark.PREMIUM_ENV_VARS}
    saved_openai = meso.openai_client.chat
    saved_gateway = meso.llm_gateway.gateway_chat
    saved_ollama = orchestrator.ollama_client.chat
    saved_frontier = meso.settings.chili_code_frontier_enabled
    for key in offline_benchmark.PREMIUM_ENV_VARS:
        os.environ.pop(key, None)
    meso.openai_client.chat = forbidden_premium_call
    meso.llm_gateway.gateway_chat = forbidden_premium_call

    def recording_local_chat(*args, **kwargs):
        result = saved_ollama(*args, **kwargs)
        model_events.append(
            {
                "ok": bool(result.ok),
                "model": str(result.model or ""),
                "latency_ms": result.latency_ms,
                "error": str(result.error or ""),
                "response": str(result.text or ""),
            }
        )
        return result

    orchestrator.ollama_client.chat = recording_local_chat
    meso.settings.chili_code_frontier_enabled = False
    db = offline_benchmark._session()
    duration = 0.0
    attempts = 0
    failure = ""
    phase_results: list[dict[str, Any]] = []
    try:
        repo = CodeRepo(path=str(root), name=f"macro-{task.task_id}", active=True)
        db.add(repo)
        db.commit()
        run = ProjectAutonomyRun(
            run_id=f"macro-{task.task_id}-{int(time.time() * 1000)}",
            repo_id=repo.id,
            prompt=task.phases[0].goal,
            status="running",
            current_stage="plan",
        )
        db.add(run)
        db.commit()
        for phase_index, phase in enumerate(task.phases):
            hashes = _prepare_phase(root, task, phase_index)
            run.prompt = phase.goal
            db.commit()
            validation_output = "validation not run"
            passed = False
            phase_attempts = 0
            context = {
                "repos": [],
                "insights": [],
                "hotspots": [],
                "disable_adaptive_investigation": True,
                "relevant_files": [
                    {
                        "file": path,
                        "symbol": Path(path).stem,
                        "relevance": 1.0 if path in task.source_files else 0.9,
                        "source": "macro_equal_context_fixture",
                    }
                    for path in sorted((*task.source_files, *_visible_tests(task, phase_index)))
                ],
            }
            started = time.perf_counter()
            plan = orchestrator.build_local_plan(db, run, repo, context=context, repo_path=root)
            duration += time.perf_counter() - started
            planned = {
                str(item.get("path") or "")
                for item in (plan.get("files") or [])
                if isinstance(item, Mapping)
            }
            if not set(task.allowed_files).issubset(planned):
                failure = "local plan omitted macro workflow files"
                break
            approved = [
                {"path": path, "action": "modify", "description": phase.goal}
                for path in task.allowed_files
            ]
            for attempt in range(1, MAX_PHASE_ATTEMPTS + 1):
                attempts += 1
                phase_attempts = attempt
                started = time.perf_counter()
                try:
                    diffs = orchestrator.generate_diffs_from_plan(
                        db,
                        run,
                        root,
                        approved,
                        validation_context=failure if attempt > 1 else None,
                    )
                    orchestrator._apply_diffs(root, diffs)
                except Exception as exc:
                    duration += time.perf_counter() - started
                    failure = f"{type(exc).__name__}: {exc}"
                    continue
                duration += time.perf_counter() - started
                passed, validation_output = _run_phase_validation(root, task, phase_index)
                if passed:
                    failure = ""
                    break
                failure = "validation failed: " + validation_output
            phase_diff = _phase_diff(root, task)
            changed = meso._changed_files(phase_diff)
            semantic = _semantic_review(task, phase, changed, phase_diff) if passed else False
            phase_results.append(
                {
                    "phase_id": phase.phase_id,
                    "passed": passed,
                    "attempts": phase_attempts,
                    "tests_unchanged": hashes == meso._hash_files(root, tuple(hashes)),
                    "semantic_review_passed": semantic,
                    "cumulative_phase_count": phase_index + 1,
                    "validation_output": validation_output,
                    "changed_files": list(changed),
                    "diff": phase_diff,
                }
            )
            if not passed:
                break
            _commit_phase(root, phase.phase_id)
            if progress:
                progress(f"{task.task_id}/local_model/{phase.phase_id} passed")
        if premium_calls:
            failure = f"premium model calls observed: {premium_calls}"
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        db.close()
        meso.openai_client.chat = saved_openai
        meso.llm_gateway.gateway_chat = saved_gateway
        orchestrator.ollama_client.chat = saved_ollama
        meso.settings.chili_code_frontier_enabled = saved_frontier
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    result = _build_result(
        task,
        "local_model",
        duration=duration,
        attempts=attempts,
        phase_results=phase_results,
        premium_calls=premium_calls,
        failure=failure,
        model_events=model_events,
    )
    if artifact_dir:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "model_events.json").write_text(json.dumps(model_events, indent=2), encoding="utf-8")
    return result


def choose_winner(results: Sequence[MacroContestantResult]) -> MacroContestantResult | None:
    eligible = [result for result in results if result.eligible]
    if not eligible:
        return None
    return max(
        eligible,
        key=lambda result: (
            result.quality_score,
            1 if result.source_kind == "local_model" and result.premium_calls == 0 else 0,
            -result.duration_seconds,
        ),
    )


def _collection_failure(result: MacroContestantResult) -> bool:
    return result.source_kind in {"codex", "claude"} and not result.final_diff.strip() and result.failure.startswith("RuntimeError:")


def _result_payload(result: MacroContestantResult) -> dict[str, Any]:
    payload = dataclasses.asdict(result)
    payload["eligible"] = result.eligible
    payload["model_events"] = [
        {key: value for key, value in event.items() if key != "response"}
        for event in result.model_events
    ]
    payload.pop("final_diff", None)
    for phase in payload.get("phase_results") or []:
        phase.pop("diff", None)
    return payload


def regrade_artifact_run(
    artifact_run: Path,
    *,
    write: bool = True,
) -> dict[str, Any]:
    """Recompute winner gates from immutable raw result artifacts without model calls."""
    summary_path = artifact_run / "summary.json"
    if not summary_path.is_file():
        raise ValueError(f"macro summary artifact is missing: {summary_path}")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary.get("schema") != SCHEMA or summary.get("evidence_mode") != "real_artifacts":
        raise ValueError("artifact run is not a real macro tournament collection")
    task_rows = summary.get("task_results") or []
    for task_row in task_rows:
        results = task_row.get("results") or []
        for result in results:
            phases = result.get("phase_results") or []
            phase_count = int(result.get("phase_count") or 0)
            phase_changes_present = (
                len(phases) == phase_count
                and phase_count > 0
                and all(bool(phase.get("changed_files")) for phase in phases)
            )
            result["required_files_changed"] = phase_changes_present
            result["phase_changes_present"] = phase_changes_present
            result["quality_score"] = quality_score(
                behavior_passed=bool(result.get("behavior_passed")),
                scope_valid=bool(result.get("scope_valid")),
                tests_unchanged=bool(result.get("tests_unchanged")),
                required_files_changed=phase_changes_present,
                continuity_valid=bool(result.get("continuity_valid")),
                semantic_review_passed=bool(result.get("semantic_review_passed")),
            )
            result["eligible"] = all(
                (
                    bool(result.get("behavior_passed")),
                    bool(result.get("scope_valid")),
                    bool(result.get("tests_unchanged")),
                    phase_changes_present,
                    bool(result.get("continuity_valid")),
                    bool(result.get("semantic_review_passed")),
                )
            )
            result["scoring_revision"] = "phase-change-evidence-v2"
            if write:
                result_path = (
                    artifact_run
                    / str(task_row.get("task_id") or "")
                    / str(result.get("source_kind") or "")
                    / "result.json"
                )
                if result_path.is_file():
                    result_path.write_text(
                        json.dumps(result, indent=2, sort_keys=True),
                        encoding="utf-8",
                    )
        eligible = [result for result in results if result.get("eligible")]
        winner = max(
            eligible,
            key=lambda result: (
                int(result.get("quality_score") or 0),
                1
                if result.get("source_kind") == "local_model"
                and result.get("premium_calls") == 0
                else 0,
                -float(result.get("duration_seconds") or 0.0),
            ),
            default=None,
        )
        task_row["winner"] = str(winner.get("source_kind")) if winner else "none"
        task_row["winner_model"] = str(winner.get("model_name")) if winner else "none"
        task_row["winner_reason"] = (
            "cumulative correctness/safety; zero-premium tie-break; then measured runtime"
            if winner
            else "no eligible contestant"
        )
    winners = Counter(str(row.get("winner") or "none") for row in task_rows)
    for source in (*SOURCE_KINDS, "none"):
        winners.setdefault(source, 0)
    summary["winner_counts"] = dict(winners)
    summary["generated_utc"] = meso._utc_now()
    summary["scoring_revision"] = "phase-change-evidence-v2"
    summary["regraded_from_raw_artifacts"] = True
    summary["status"] = (
        "passed"
        if int(summary.get("tasks") or 0) >= 3
        and not summary.get("collection_failures")
        and winners["none"] == 0
        else "failed"
    )
    if write:
        summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return summary


def run_tournament(
    *,
    tasks: Sequence[MacroTask] | None = None,
    source_kinds: Sequence[str] = SOURCE_KINDS,
    frontier_call: FrontierCall = meso._default_frontier_call,
    timeout_seconds: int = 900,
    max_budget_usd: float = 10.0,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
    run_id: str | None = None,
    write: bool = True,
    progress: Progress | None = None,
) -> dict[str, Any]:
    selected = tuple(tasks or default_tasks())
    sources = tuple(dict.fromkeys(str(value).strip() for value in source_kinds))
    invalid = sorted(set(sources) - set(SOURCE_KINDS))
    if invalid:
        raise ValueError("unsupported source kinds: " + ", ".join(invalid))
    clean_run_id = meso._safe_id(run_id or f"macro-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    if write:
        run_dir = artifact_root / clean_run_id
        if run_dir.exists():
            raise ValueError(f"artifact run already exists: {run_dir}")
        run_dir.mkdir(parents=True, exist_ok=False)
        cleanup = None
    else:
        cleanup = tempfile.TemporaryDirectory(prefix="chili_macro_tournament_")
        run_dir = Path(cleanup.name)
    all_results: list[MacroContestantResult] = []
    task_rows = []
    try:
        for task in selected:
            results = []
            for source_kind in sources:
                if progress:
                    progress(f"{task.task_id}/{source_kind} start")
                with tempfile.TemporaryDirectory(prefix=f"macro_{task.task_id}_{source_kind}_") as tmp:
                    root = Path(tmp)
                    _init_task_repo(task, root)
                    artifact_dir = run_dir / task.task_id / source_kind if write else None
                    if source_kind == "local_model":
                        result = run_chili_contestant(task, root, artifact_dir=artifact_dir, progress=progress)
                    else:
                        result = run_frontier_contestant(
                            task,
                            source_kind,
                            root,
                            call=frontier_call,
                            timeout_seconds=timeout_seconds,
                            max_budget_usd=max_budget_usd,
                            artifact_dir=artifact_dir,
                            progress=progress,
                        )
                    if artifact_dir:
                        artifact_dir.mkdir(parents=True, exist_ok=True)
                        (artifact_dir / "result.json").write_text(json.dumps(_result_payload(result), indent=2, sort_keys=True), encoding="utf-8")
                        (artifact_dir / "final.diff").write_text(result.final_diff, encoding="utf-8")
                    results.append(result)
                    all_results.append(result)
                if progress:
                    progress(f"{task.task_id}/{source_kind} complete score={result.quality_score} phases={result.phases_passed}/{result.phase_count}")
            winner = choose_winner(results)
            task_rows.append(
                {
                    "task_id": task.task_id,
                    "title": task.title,
                    "winner": winner.source_kind if winner else "none",
                    "winner_model": winner.model_name if winner else "none",
                    "winner_reason": "cumulative correctness/safety; zero-premium tie-break; then measured runtime" if winner else "no eligible contestant",
                    "results": [_result_payload(result) for result in results],
                }
            )
        winners = Counter(str(row["winner"]) for row in task_rows)
        for source in (*SOURCE_KINDS, "none"):
            winners.setdefault(source, 0)
        failures = [
            f"{result.task_id}/{result.source_kind}: {result.failure}"
            for result in all_results
            if _collection_failure(result)
        ]
        expected = len(selected) * len(sources)
        status = (
            "passed"
            if len(selected) >= 3
            and set(sources) == set(SOURCE_KINDS)
            and len(all_results) == expected
            and not failures
            and winners["none"] == 0
            and all(result.model_name == MODEL_NAMES[result.source_kind] for result in all_results)
            else "failed"
        )
        summary = {
            "schema": SCHEMA,
            "generated_utc": meso._utc_now(),
            "status": status,
            "evidence_mode": "real_artifacts",
            "run_id": clean_run_id,
            "artifact_root": str(run_dir),
            "tasks": len(selected),
            "phases_per_task": 3,
            "source_kinds": list(sources),
            "model_names": {source: MODEL_NAMES[source] for source in sources},
            "winner_counts": dict(winners),
            "runtime_measurements": {"measured": len(all_results), "unmeasured": 0},
            "collection_failures": failures,
            "premium_independent_local_results": sum(
                1 for result in all_results if result.source_kind == "local_model" and result.premium_calls == 0
            ),
            "task_results": task_rows,
            "safety": (
                "isolated temporary repositories only; Fable 5 and Codex 5.6 Sol are benchmark "
                "opponents; premium routes are fatal inside CHILI; no real source publication, "
                "deployment, runtime, broker, or live-trading action"
            ),
        }
        if write:
            (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
        return summary
    finally:
        if cleanup is not None:
            cleanup.cleanup()


def _counts_text(counts: Mapping[str, Any]) -> str:
    return ", ".join(f"{source}={int(counts.get(source, 0) or 0)}" for source in ("local_model", "codex", "claude", "none"))


def render_report(summary: Mapping[str, Any]) -> str:
    runtime = summary.get("runtime_measurements") or {}
    lines = [
        "# CHILI Macro Long-Horizon Tournament",
        "",
        f"- Schema: {SCHEMA}",
        f"- Generated UTC: {summary.get('generated_utc', '')}",
        f"- Status: {summary.get('status', 'failed')}",
        f"- Evidence mode: {summary.get('evidence_mode', 'missing')}",
        f"- Run id: {summary.get('run_id', 'missing')}",
        f"- Tasks: {summary.get('tasks', 0)}",
        f"- Phases per task: {summary.get('phases_per_task', 0)}",
        f"- Scoring revision: {summary.get('scoring_revision', 'phase-change-evidence-v2')}",
        f"- Regraded from raw artifacts: {str(bool(summary.get('regraded_from_raw_artifacts'))).lower()}",
        f"- Source kinds: {', '.join(summary.get('source_kinds') or [])}",
        f"- Winner counts: {_counts_text(summary.get('winner_counts') or {})}",
        f"- Collection failures: {len(summary.get('collection_failures') or [])}",
        f"- Runtime measurements: measured={runtime.get('measured', 0)}, unmeasured={runtime.get('unmeasured', 0)}",
        f"- Premium-independent local results: {summary.get('premium_independent_local_results', 0)}/{summary.get('tasks', 0)}",
        "- Winner rule: cumulative correctness and safety first; on an exact quality tie, zero-premium operational independence; then measured runtime.",
        f"- Safety: {summary.get('safety', '')}",
        "",
        "| Task | Winner | Model | Local | Codex | Fable 5 | Evidence |",
        "| --- | --- | --- | ---: | ---: | ---: | --- |",
    ]
    for task_row in summary.get("task_results") or []:
        by_source = {str(item.get("source_kind")): item for item in task_row.get("results") or []}
        evidence = []
        for source in SOURCE_KINDS:
            result = by_source.get(source) or {}
            evidence.append(
                f"{source}:phases={result.get('phases_passed', 0)}/{result.get('phase_count', 0)},"
                f"quality={result.get('quality_score', 0)},premium_calls={result.get('premium_calls', 'n/a')},"
                f"seconds={result.get('duration_seconds', 0)}"
            )
        lines.append(
            "| "
            + " | ".join(
                [
                    str(task_row.get("task_id") or ""),
                    str(task_row.get("winner") or "none"),
                    str(task_row.get("winner_model") or "none"),
                    str((by_source.get("local_model") or {}).get("quality_score", 0)),
                    str((by_source.get("codex") or {}).get("quality_score", 0)),
                    str((by_source.get("claude") or {}).get("quality_score", 0)),
                    "; ".join(evidence),
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run equal-goal CHILI/Codex/Fable macro long-horizon tournament.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--run-id")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--max-budget-usd", type=float, default=10.0)
    parser.add_argument(
        "--regrade-artifact-run",
        type=Path,
        help="Recompute fair quality gates from an existing raw artifact run without model calls.",
    )
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.regrade_artifact_run is not None:
        summary = regrade_artifact_run(
            args.regrade_artifact_run,
            write=not args.no_write,
        )
    else:
        summary = run_tournament(
            timeout_seconds=max(60, args.timeout_seconds),
            max_budget_usd=max(0.1, args.max_budget_usd),
            artifact_root=args.artifact_root,
            run_id=args.run_id,
            write=not args.no_write,
            progress=lambda message: print(f"[macro-tournament] {message}", file=sys.stderr, flush=True),
        )
    report = render_report(summary)
    if not args.no_write:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(report, end="")
    return 0 if summary.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
