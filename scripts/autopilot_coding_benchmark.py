from __future__ import annotations

import argparse
import dataclasses
import importlib.metadata
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
DEFAULT_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "CODING_BENCHMARK_SCORECARD.md"
BENCHMARK_SCHEMA_VERSION = "chili.coding-benchmark.v1"
TARGET_SCORE = 90
MIN_SCENARIOS = 6
OUTPUT_SNIPPET_CHARS = 720
OUTPUT_LINE_SNIPPET_CHARS = 420
DEFAULT_HEARTBEAT_SECONDS = 20.0
TIMEOUT_RETRY_MULTIPLIER = 2
TIMEOUT_RETRY_MAX_SECONDS = 900
DEFAULT_SOURCE_QUIET_POLL_SECONDS = 1.0
DEFAULT_SOURCE_QUIET_TIMEOUT_SECONDS = 300.0
DEFAULT_SOURCE_QUIET_LEASE_SECONDS = 7200.0
SOURCE_QUIET_LEASE_ENV = "CHILI_BENCHMARK_SOURCE_LEASE_ID"
SOURCE_QUIET_LEASE_SCHEMA_VERSION = "chili.source-quiet-benchmark-lease.v1"
DEFAULT_SOURCE_QUIET_LEASE_PATH = (
    REPO_ROOT / "project_ws" / "AgentOps" / "SOURCE_QUIET_BENCHMARK_LEASE.json"
)
ENVIRONMENT_BLOCKED_STATUS = "environment_blocked"
PYTEST_SUPPORTED_SPEC = "pytest>=8.2,<9"
ENVIRONMENT_RECOVERY_GUIDANCE = (
    f"inspect the row evidence; for pytest-backed rows create or select a repo-local "
    f"Python environment with {PYTEST_SUPPORTED_SPEC}, otherwise wait for active "
    "benchmark/build workers to drain, verify no process owns the tool lock, then "
    "rerun the same scenario before judging coding quality"
)
SOURCE_STABILITY_PREVIEW_LIMIT = 5
SOURCE_STABILITY_ROOTS = (
    "app",
    "tests",
    "scripts",
    "chili_mobile/lib",
    "chili_mobile/test",
)
SOURCE_STABILITY_SUFFIXES = (
    ".css",
    ".dart",
    ".html",
    ".js",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
)
SOURCE_STABILITY_SKIP_DIRS = {
    ".dart_tool",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    "__pycache__",
    "build",
    "node_modules",
    "project_ws",
}
REQUIRED_CAPABILITIES = (
    "robust plan JSON extraction",
    "request preflight safety",
    "unified diff validation",
    "merge-conflict marker rejection",
    "edit prompt related context",
    "edit prompt dependency context",
    "edit prompt dependency contract tests",
    "edit prompt ownership context",
    "edit prompt recent failure context",
    "edit prompt nearby convention context",
    "edit prompt public symbol impact context",
    "edit prompt impact contract tests",
    "edit prompt multi-hop impact context",
    "edit prompt ranked multi-hop impact context",
    "edit prompt indexed call-path context",
    "edit prompt persisted call-edge context",
    "evidence-enriched planning context",
    "three-file contract planning",
    "plan success and validation contract",
    "adaptive read-only repository investigation",
    "model-directed repository tools",
    "investigation evidence provenance",
    "parallel read-only investigation lanes",
    "database-session isolation across lanes",
    "deterministic parallel evidence ordering",
    "evidence-gated trajectory storage",
    "validated trajectory retrieval",
    "planning and repair precedent context",
    "production lifecycle event streaming",
    "durable event provenance",
    "streaming callback failure isolation",
    "event-assisted SSE wakeup",
    "premium-disconnected local autonomy",
    "external frontier benchmark-only boundary",
    "local model plan-and-edit contract",
    "real offline local-model project repair",
    "zero-premium-call runtime proof",
    "atomic coordinated multi-file edits",
    "coordinated patch scope validation",
    "cross-file contract repair context",
    "code search persisted caller lookup",
    "changed-file syntax evidence",
    "behavior-aware test discovery",
    "patch-specific evidence gate",
    "targeted behavior evidence gate",
    "structured validation repair context",
    "semantic patch review gate",
    "return/exception contract evidence",
    "domain-specific behavior evidence",
    "invariant-specific trading evidence",
    "executable trading invariant replay",
    "real trading business invariant fixtures",
    "multi-step trading incident replay",
    "db-backed trading incident replay",
    "synthetic repo repair replay",
    "multi-iteration repair replay",
    "real CHILI regression replay",
    "expanded real CHILI regression corpus",
    "trading import cycle regression",
    "real CHILI candidate bakeoff",
    "model candidate handoff prompt pack",
    "frontier source-specific prompt pack",
    "frontier model evidence setup safety",
    "frontier source collection packet",
    "frontier source runner automation",
    "exact Codex 5.6 source collection",
    "exact Fable 5 source collection",
    "measured frontier runtime provenance",
    "frontier source live availability probe",
    "frontier source evidence recording",
    "frontier source suite response import",
    "frontier response drop parsing",
    "frontier evidence preflight recovery routing",
    "local model evidence recording",
    "compact local model candidate collection",
    "local model candidate suite collection",
    "parse-failure repair continuation",
    "early test-derived deterministic repair",
    "measured model-plus-repair latency",
    "model output artifact collection",
    "model run drop collector",
    "model candidate provenance gate",
    "frontier model evidence intake orchestration",
    "real model shadow evidence gate",
    "real shadow evidence mode gate",
    "external model artifact intake",
    "multi-source model candidate tournament",
    "real model tournament evidence mode gate",
    "frontier patch bakeoff grading",
    "execution worktree isolation",
    "scoped autonomous patch staging",
    "operator checkout preservation",
    "reviewed branch acceptance lineage",
    "latest-attempt workflow lineage",
    "pre-merge evidence lineage",
    "visual evidence merge gate",
    "execution handoff packet",
    "execution worktree lifecycle cleanup",
    "execution publication export handoff",
    "execution pr publication revalidation",
    "execution pr check monitoring",
    "execution pr publication receipt gate",
    "execution pr repair routing",
    "execution pr repair execution",
    "execution pr review feedback ingestion",
    "execution post-repair publication handoff",
    "execution pr line-thread ingestion",
    "execution post-repair pr recheck",
    "hosted pr repair artifact replay",
    "hosted pr repair provenance gate",
    "hosted pr repair artifact inventory gate",
    "hosted pr repair collection packet",
    "hosted pr repair evidence collector",
    "hosted pr repair artifact assembler",
    "real hosted pr repair inventory mode gate",
    "blast-radius change control",
    "patch self-review minimality",
    "operator-visible benchmark gate",
    "frontier readiness gap audit",
    "frontier gap matrix",
    "current source freshness gate",
    "benchmark-driven model promotion",
    "failed-closed merge control",
    "model patch rejection evidence",
    "real report replay grading",
    "task-level replay grading",
    "archived task semantic replay",
)


@dataclasses.dataclass(frozen=True)
class BenchmarkScenario:
    scenario_id: str
    name: str
    category: str
    command: tuple[str, ...]
    cwd: Path
    timeout_seconds: int = 180
    capabilities: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class BenchmarkResult:
    scenario: BenchmarkScenario
    status: str
    exit_code: int | None
    duration_seconds: float
    evidence: str

    @property
    def passed(self) -> bool:
        return self.status == "passed"


def _guarded_pytest_scenario(scenario: BenchmarkScenario) -> BenchmarkScenario:
    command = scenario.command
    if len(command) >= 3 and command[1:3] == ("-m", "pytest"):
        return dataclasses.replace(
            scenario,
            command=(
                command[0],
                "scripts/pytest_adaptive.py",
                "run",
                *command[3:],
            ),
        )
    return scenario


def _benchmark_python(repo_root: Path = REPO_ROOT) -> str:
    try:
        from scripts import pytest_adaptive

        contract = pytest_adaptive.pytest_runtime_contract(repo_root)
    except Exception:
        return sys.executable
    if (
        contract.actual != "missing"
        and _pytest_version_supported(contract.actual)
        and contract.isolation_status == "isolated"
        and not contract.missing_imports
    ):
        return contract.python
    return sys.executable


def default_scenarios(
    repo_root: Path = REPO_ROOT,
    *,
    include_mobile: bool = False,
) -> list[BenchmarkScenario]:
    python = _benchmark_python(repo_root)
    mobile = repo_root / "chili_mobile"
    flutter = _tool_command("flutter")
    scenarios = [
        BenchmarkScenario(
            "core-python-compile",
            "Core Python compile",
            "syntax",
            (
                python,
                "-c",
                "from pathlib import Path; [compile(Path(path).read_text(encoding='utf-8'), path, 'exec') for path in ('app/services/project_autonomy/orchestrator.py','app/services/code_brain/agent.py','app/services/coding_task/validator_runner.py','app/services/coding_task/execution_loop.py')]",
            ),
            repo_root,
            timeout_seconds=120,
            capabilities=("changed-code syntax guard", "import-time regression smoke"),
        ),
        BenchmarkScenario(
            "code-agent-plan-safety",
            "Code-agent plan parsing and repo boundary safety",
            "planning",
            (
                python,
                "scripts/autopilot_code_agent_unit_benchmark.py",
                "--suite",
                "plan-safety",
            ),
            repo_root,
            timeout_seconds=180,
            capabilities=("robust plan JSON extraction", "repo-relative read boundary"),
        ),
        BenchmarkScenario(
            "code-agent-request-preflight-safety",
            "Code-agent request preflight safety",
            "planning",
            (
                python,
                "scripts/autopilot_code_agent_unit_benchmark.py",
                "--suite",
                "request-preflight-safety",
            ),
            repo_root,
            timeout_seconds=180,
            capabilities=(
                "request preflight safety",
                "destructive action fail-closed",
                "clarification gate before model planning",
            ),
        ),
        BenchmarkScenario(
            "code-agent-diff-safety",
            "Code-agent diff target safety",
            "patch",
            (
                python,
                "scripts/autopilot_code_agent_unit_benchmark.py",
                "--suite",
                "diff-safety",
            ),
            repo_root,
            timeout_seconds=180,
            capabilities=(
                "unified diff validation",
                "path traversal rejection",
                "explicit new-file intent",
                "merge-conflict marker rejection",
            ),
        ),
        BenchmarkScenario(
            "code-agent-related-context",
            "Code-agent related edit context",
            "planning",
            (
                python,
                "scripts/autopilot_code_agent_unit_benchmark.py",
                "--suite",
                "related-context",
            ),
            repo_root,
            timeout_seconds=300,
            capabilities=(
                "edit prompt related context",
                "edit prompt dependency context",
                "edit prompt dependency contract tests",
                "edit prompt ownership context",
                "edit prompt recent failure context",
                "edit prompt nearby convention context",
                "edit prompt public symbol impact context",
                "edit prompt impact contract tests",
                "edit prompt multi-hop impact context",
                "edit prompt ranked multi-hop impact context",
                "edit prompt indexed call-path context",
                "edit prompt persisted call-edge context",
                "call-site and test context",
            ),
        ),
        BenchmarkScenario(
            "code-search-persisted-callers",
            "Code search persisted caller lookup",
            "planning",
            (
                python,
                "-m",
                "pytest",
                "tests/test_code_brain_llm_routing.py",
                "-q",
                "-k",
                "code_search_routes_llm_through_cacheable_code_search_purpose or code_agent_source_has_no_direct_openai_fallback",
            ),
            repo_root,
            timeout_seconds=180,
            capabilities=(
                "code search persisted caller lookup",
                "code call-edge schema migration",
            ),
        ),
        BenchmarkScenario(
            "autopilot-coordinated-multi-file-edit",
            "Autopilot coordinated multi-file edit",
            "patch",
            (
                python,
                "-m",
                "pytest",
                "tests/test_project_autonomy_service.py::test_generate_diffs_uses_atomic_coordinated_patch_for_multi_file_contract",
                "tests/test_project_autonomy_service.py::test_generate_diffs_accepts_structured_exact_replacements",
                "tests/test_project_autonomy_service.py::test_structured_replacements_reject_inserted_return_before_existing_return",
                "tests/test_project_autonomy_service.py::test_structured_replacements_restore_uniquely_matched_indentation",
                "tests/test_project_autonomy_service.py::test_coordinated_structured_edit_repairs_commented_invalid_json",
                "tests/test_project_autonomy_service.py::test_apply_diffs_recounts_valid_model_patch_hunks",
                "tests/test_project_autonomy_service.py::test_generate_diffs_canonicalizes_real_multi_file_model_response",
                "tests/test_project_autonomy_service.py::test_generate_diffs_rejects_unplanned_coordinated_target_before_single_file_fallback",
                "tests/test_project_autonomy_service.py::test_generate_diffs_rejects_single_file_response_for_another_target",
                "tests/test_project_autonomy_service.py::test_generate_diffs_rejects_cross_file_old_header_even_when_new_target_is_approved",
                "tests/test_project_autonomy_service.py::test_generate_diffs_rejects_single_file_dead_code_insertion",
                "tests/test_project_autonomy_service.py::test_generate_diffs_threads_structured_failure_into_coordinated_repair_prompt",
                "tests/test_project_autonomy_service.py::test_generate_diffs_allows_scoped_approved_repair_subset",
                "-q",
            ),
            repo_root,
            timeout_seconds=240,
            capabilities=(
                "atomic coordinated multi-file edits",
                "coordinated patch scope validation",
                "cross-file contract repair context",
            ),
        ),
        BenchmarkScenario(
            "autopilot-evidence-enriched-planning",
            "Autopilot evidence-enriched planning",
            "planning",
            (
                python,
                "-m",
                "pytest",
                "tests/test_project_autonomy_service.py::test_autonomy_plan_prompt_includes_bounded_candidate_source_evidence",
                "tests/test_project_autonomy_service.py::test_plan_files_normalize_existing_file_action_synonyms",
                "tests/test_project_autonomy_service.py::test_local_plan_preserves_reasoning_and_validation_contract",
                "tests/test_project_autonomy_service.py::test_low_score_plan_revision_adds_evidence_backed_test_alternative",
                "tests/test_project_autonomy_service.py::test_architect_review_requires_and_accepts_evidence_backed_test_scope",
                "tests/test_project_autonomy_service.py::test_architect_review_requires_explicit_named_collaborator_and_revision_adds_it",
                "-q",
            ),
            repo_root,
            timeout_seconds=240,
            capabilities=(
                "evidence-enriched planning context",
                "three-file contract planning",
                "plan success and validation contract",
            ),
        ),
        BenchmarkScenario(
            "autopilot-adaptive-plan-investigation",
            "Autopilot adaptive plan investigation",
            "planning",
            (
                python,
                "-m",
                "pytest",
                "tests/test_project_autonomy_service.py::test_plan_investigation_tools_reach_lower_symbol_callers_and_tests_without_mutation",
                "tests/test_project_autonomy_service.py::test_plan_investigation_parallelizes_filesystem_reads_without_sharing_db_session",
                "tests/test_project_autonomy_service.py::test_build_local_plan_runs_adaptive_model_directed_investigation",
                "-q",
            ),
            repo_root,
            timeout_seconds=240,
            capabilities=(
                "adaptive read-only repository investigation",
                "model-directed repository tools",
                "investigation evidence provenance",
                "parallel read-only investigation lanes",
                "database-session isolation across lanes",
                "deterministic parallel evidence ordering",
            ),
        ),
        BenchmarkScenario(
            "autopilot-validated-trajectory-learning",
            "Autopilot validated trajectory learning",
            "planning",
            (
                python,
                "-m",
                "pytest",
                "tests/test_project_autonomy_service.py::test_learning_precedents_require_validation_and_merge_readiness",
                "tests/test_project_autonomy_service.py::test_reviewed_plan_retrieves_validated_learning_precedent",
                "-q",
            ),
            repo_root,
            timeout_seconds=240,
            capabilities=(
                "evidence-gated trajectory storage",
                "validated trajectory retrieval",
                "planning and repair precedent context",
            ),
        ),
        BenchmarkScenario(
            "autopilot-production-event-streaming",
            "Autopilot production event streaming",
            "orchestration",
            (
                python,
                "-m",
                "pytest",
                "tests/test_project_autonomy_service.py::test_run_autonomy_sync_streams_persisted_lifecycle_events",
                "tests/test_project_autonomy_service.py::test_run_autonomy_sync_ignores_stream_callback_failures",
                "tests/test_brain_project_autonomy_routes.py::test_autonomy_worker_publishes_live_event_pulses",
                "-q",
            ),
            repo_root,
            timeout_seconds=180,
            capabilities=(
                "production lifecycle event streaming",
                "durable event provenance",
                "streaming callback failure isolation",
                "event-assisted SSE wakeup",
            ),
        ),
        BenchmarkScenario(
            "autopilot-premium-independent-autonomy",
            "Autopilot premium-independent local autonomy",
            "orchestration",
            (
                python,
                "-m",
                "pytest",
                "tests/test_project_autonomy_service.py::test_project_autonomy_declares_and_enforces_local_only_dependency_boundary",
                "tests/test_project_autonomy_service.py::test_build_local_plan_uses_bounded_warm_ollama_options",
                "tests/test_project_autonomy_service.py::test_generate_diffs_uses_atomic_coordinated_patch_for_multi_file_contract",
                "-q",
            ),
            repo_root,
            timeout_seconds=240,
            capabilities=(
                "premium-disconnected local autonomy",
                "external frontier benchmark-only boundary",
                "local model plan-and-edit contract",
            ),
        ),
        BenchmarkScenario(
            "autopilot-offline-project-autonomy",
            "Autopilot real offline project autonomy",
            "orchestration",
            (
                python,
                "scripts/autopilot_offline_project_autonomy_benchmark.py",
                "--no-write",
            ),
            repo_root,
            timeout_seconds=360,
            capabilities=(
                "premium-disconnected local autonomy",
                "real offline local-model project repair",
                "zero-premium-call runtime proof",
                "local model plan-and-edit contract",
            ),
        ),
        BenchmarkScenario(
            "validator-changed-file-ast",
            "Validator changed-file AST coverage",
            "validation",
            (
                python,
                "-m",
                "pytest",
                "tests/test_coding_validator_safety.py",
                "tests/test_project_autonomy_service.py::test_implementation_phase_runs_real_validation_contract",
                "-q",
                "-k",
                "ast_syntax_does_not_mutate_source_file or ast_syntax_targets_changed_python_files_and_records_scope or ast_syntax_rejects_changed_path_outside_worktree or subprocess_timeout_kills_long_running_step or implementation_phase_runs_real_validation_contract",
            ),
            repo_root,
            timeout_seconds=180,
            capabilities=(
                "changed-file syntax evidence",
                "validation metadata threading",
                "merge-conflict marker rejection",
            ),
        ),
        BenchmarkScenario(
            "validator-targeted-test-discovery",
            "Validator targeted test discovery",
            "validation",
            (
                python,
                "-m",
                "pytest",
                "tests/test_coding_validator_safety.py",
                "-q",
                "-k",
                "pytest_targeted_skips_when_safe_test_database_is_not_configured",
            ),
            repo_root,
            timeout_seconds=180,
            capabilities=("behavior-aware test discovery", "validation test-selection metadata"),
        ),
        BenchmarkScenario(
            "snapshot-diff-path-safety",
            "Snapshot diff path safety",
            "patch",
            (
                python,
                "-m",
                "pytest",
                "tests/test_coding_validator_safety.py",
                "-q",
                "-k",
                "disallowed_step_raises or subprocess_safe_env_strips_arbitrary_vars",
            ),
            repo_root,
            timeout_seconds=180,
            capabilities=("snapshot metadata enforcement", "apply-time path traversal rejection"),
        ),
        BenchmarkScenario(
            "coding-execution-worktree-isolation",
            "Coding execution worktree isolation",
            "patch",
            (
                python,
                "-m",
                "pytest",
                "tests/test_coding_execution_loop_llm_cost.py",
                "-q",
                "-k",
                "worktree_creation or execution_worktree_preserves_operator_dirty_state or execution_loop_runs_in_worktree_and_preserves_operator_checkout or stages_only_generated or reads_applies or apply_diffs_rejects_path_traversal or acceptance_preflight_binds_branch_to_validated_file_set or handoff or lifecycle or publication_export or pr_publication or pr_status or pr_repair",
            ),
            repo_root,
            timeout_seconds=180,
            capabilities=(
                "execution worktree isolation",
                "scoped autonomous patch staging",
                "execution handoff packet",
                "execution worktree lifecycle cleanup",
                "execution publication export handoff",
                "execution pr publication revalidation",
                "execution pr check monitoring",
                "execution pr publication receipt gate",
                "execution pr repair routing",
                "execution pr repair execution",
                "execution pr review feedback ingestion",
                "execution post-repair publication handoff",
                "execution pr line-thread ingestion",
                "execution post-repair pr recheck",
                "operator checkout preservation",
                "reviewed branch acceptance lineage",
            ),
        ),
        BenchmarkScenario(
            "coding-workflow-attempt-lineage",
            "Coding workflow latest-attempt lineage",
            "quality",
            (
                python,
                "-m",
                "pytest",
                "tests/test_workflow_state.py",
                "-q",
                "-k",
                "dry_run_then_applied_then_validated or new_failed_apply_supersedes_old_success or new_failed_validation_supersedes_old_success or new_snapshot_invalidates_old_apply_trajectory",
            ),
            repo_root,
            timeout_seconds=180,
            capabilities=("latest-attempt workflow lineage",),
        ),
        BenchmarkScenario(
            "autopilot-validation-evidence",
            "Autopilot validation evidence gate",
            "quality",
            (
                python,
                "-m",
                "pytest",
                "tests/test_project_autonomy_service.py",
                "-q",
                "-k",
                "validation_merge_evidence or policy_blocked_npm_script_blocks_validation_passed or attempt_merge_revalidates_precommit_evidence_lineage",
            ),
            repo_root,
            timeout_seconds=240,
            capabilities=("patch-specific evidence gate", "policy blocker propagation", "pre-merge evidence lineage"),
        ),
        BenchmarkScenario(
            "autopilot-behavior-evidence",
            "Autopilot targeted behavior evidence gate",
            "quality",
            (
                python,
                "-m",
                "pytest",
                "tests/test_project_autonomy_service.py",
                "-q",
                "-k",
                "behavior_validation_evidence or implementation_blocks_behavior_evidence_failure_before_commit",
            ),
            repo_root,
            timeout_seconds=240,
            capabilities=("targeted behavior evidence gate", "pre-commit validation block"),
        ),
        BenchmarkScenario(
            "autopilot-repair-context",
            "Autopilot structured validation repair context",
            "quality",
            (
                python,
                "-m",
                "pytest",
                "tests/test_project_autonomy_service.py",
                "-q",
                "-k",
                "validation_repair_context or implementation_records_structured_repair_context",
            ),
            repo_root,
            timeout_seconds=240,
            capabilities=("structured validation repair context", "repair prompt failure evidence"),
        ),
        BenchmarkScenario(
            "autopilot-semantic-review",
            "Autopilot semantic patch review gate",
            "quality",
            (
                python,
                "-m",
                "pytest",
                "tests/test_project_autonomy_service.py",
                "-q",
                "-k",
                "semantic_patch_review or implementation_blocks_public_contract_change or attempt_merge_revalidates_precommit_evidence_lineage",
            ),
            repo_root,
            timeout_seconds=240,
            capabilities=(
                "semantic patch review gate",
                "public contract evidence gate",
                "return/exception contract evidence",
            ),
        ),
        BenchmarkScenario(
            "autopilot-visual-evidence-merge-gate",
            "Autopilot visual evidence merge gate",
            "quality",
            (
                python,
                "-m",
                "pytest",
                "tests/test_project_autonomy_service.py::test_implementation_blocks_visible_ui_without_visual_evidence_before_commit",
                "-q",
            ),
            repo_root,
            timeout_seconds=180,
            capabilities=("visual evidence merge gate",),
        ),
        BenchmarkScenario(
            "autopilot-domain-behavior",
            "Autopilot trading/runtime domain behavior gate",
            "quality",
            (
                python,
                "-m",
                "pytest",
                "tests/test_project_autonomy_service.py",
                "-q",
                "-k",
                "domain_behavior or implementation_blocks_trading_change",
            ),
            repo_root,
            timeout_seconds=240,
            capabilities=(
                "domain-specific behavior evidence",
                "invariant-specific trading evidence",
                "trading runtime evidence gate",
            ),
        ),
        BenchmarkScenario(
            "autopilot-trading-invariant-replay",
            "Autopilot trading invariant replay",
            "quality",
            (
                python,
                "scripts/autopilot_trading_invariant_replay_benchmark.py",
                "--no-write",
            ),
            repo_root,
            timeout_seconds=120,
            capabilities=(
                "executable trading invariant replay",
                "invariant-specific trading evidence",
                "trading runtime evidence gate",
            ),
        ),
        BenchmarkScenario(
            "autopilot-trading-business-invariants",
            "Autopilot trading business invariants",
            "quality",
            (
                python,
                "scripts/autopilot_trading_business_invariant_benchmark.py",
                "--no-write",
            ),
            repo_root,
            timeout_seconds=120,
            capabilities=(
                "real trading business invariant fixtures",
                "invariant-specific trading evidence",
                "trading runtime evidence gate",
            ),
        ),
        BenchmarkScenario(
            "autopilot-trading-incident-replay",
            "Autopilot trading incident replay",
            "quality",
            (
                python,
                "scripts/autopilot_trading_incident_replay_benchmark.py",
                "--no-write",
            ),
            repo_root,
            timeout_seconds=120,
            capabilities=(
                "multi-step trading incident replay",
                "real trading business invariant fixtures",
                "trading runtime evidence gate",
            ),
        ),
        BenchmarkScenario(
            "autopilot-trading-db-incident-replay",
            "Autopilot DB-backed trading incident replay",
            "quality",
            (
                python,
                "scripts/autopilot_trading_db_incident_replay_benchmark.py",
                "--no-write",
            ),
            repo_root,
            timeout_seconds=120,
            capabilities=(
                "db-backed trading incident replay",
                "multi-step trading incident replay",
                "trading runtime evidence gate",
            ),
        ),
        BenchmarkScenario(
            "autopilot-synthetic-repo-repair-replay",
            "Autopilot synthetic repo repair replay",
            "quality",
            (
                python,
                "scripts/autopilot_synthetic_repo_repair_benchmark.py",
                "--no-write",
            ),
            repo_root,
            timeout_seconds=180,
            capabilities=(
                "synthetic repo repair replay",
                "must-ask clarification replay",
                "side-effect repair gate",
            ),
        ),
        BenchmarkScenario(
            "autopilot-multistep-repair-loop-replay",
            "Autopilot multi-step repair loop replay",
            "quality",
            (
                python,
                "scripts/autopilot_multistep_repair_loop_benchmark.py",
                "--no-write",
            ),
            repo_root,
            timeout_seconds=180,
            capabilities=(
                "multi-iteration repair replay",
                "validation repair context replay",
                "chili-derived bug slice replay",
            ),
        ),
        BenchmarkScenario(
            "autopilot-hosted-pr-repair-artifact-replay",
            "Autopilot hosted PR repair artifact replay",
            "quality",
            (
                python,
                "scripts/autopilot_hosted_pr_repair_artifact_benchmark.py",
                "--no-write",
            ),
            repo_root,
            timeout_seconds=120,
            capabilities=(
                "hosted pr repair artifact replay",
                "hosted pr repair provenance gate",
                "hosted pr repair artifact inventory gate",
                "execution pr line-thread ingestion",
                "execution post-repair pr recheck",
            ),
        ),
        BenchmarkScenario(
            "autopilot-hosted-pr-repair-evidence-mode-gate",
            "Autopilot hosted PR repair evidence mode gate",
            "quality",
            (
                python,
                "-m",
                "pytest",
                "tests/test_project_autonomy_service.py::test_coding_benchmark_scorecard_rejects_hosted_pr_repair_self_test_mode",
                "tests/test_autopilot_hosted_pr_repair_artifact_benchmark.py::test_hosted_pr_repair_artifact_cli_real_inventory_writes_promotion_ready_scorecard",
                "-q",
            ),
            repo_root,
            timeout_seconds=120,
            capabilities=(
                "hosted pr repair artifact replay",
                "hosted pr repair artifact inventory gate",
                "real hosted pr repair inventory mode gate",
                "frontier-agent parity regression gate",
            ),
        ),
        BenchmarkScenario(
            "autopilot-hosted-pr-repair-collection-packet",
            "Autopilot hosted PR repair collection packet",
            "quality",
            (
                python,
                "-m",
                "pytest",
                "tests/test_autopilot_hosted_pr_repair_collection_packet.py",
                "tests/test_autopilot_hosted_pr_repair_artifact_benchmark.py::test_hosted_pr_repair_artifact_accepts_hosted_ci_failure_shape",
                "-q",
            ),
            repo_root,
            timeout_seconds=120,
            capabilities=(
                "hosted pr repair collection packet",
                "hosted pr repair artifact replay",
                "real hosted pr repair inventory mode gate",
            ),
        ),
        BenchmarkScenario(
            "autopilot-hosted-pr-repair-artifact-assembler",
            "Autopilot hosted PR repair artifact assembler",
            "quality",
            (
                python,
                "-m",
                "pytest",
                "tests/test_autopilot_hosted_pr_repair_artifact_assembler.py",
                "-q",
            ),
            repo_root,
            timeout_seconds=120,
            capabilities=(
                "hosted pr repair artifact assembler",
                "hosted pr repair artifact replay",
                "real hosted pr repair inventory mode gate",
            ),
        ),
        BenchmarkScenario(
            "autopilot-hosted-pr-repair-evidence-collector",
            "Autopilot hosted PR repair evidence collector",
            "quality",
            (
                python,
                "-m",
                "pytest",
                "tests/test_autopilot_hosted_pr_repair_evidence_collector.py",
                "-q",
            ),
            repo_root,
            timeout_seconds=120,
            capabilities=(
                "hosted pr repair evidence collector",
                "hosted pr repair collection packet",
                "real hosted pr repair inventory mode gate",
            ),
        ),
        BenchmarkScenario(
            "autopilot-blast-radius-safety",
            "Autopilot blast-radius safety",
            "quality",
            (
                python,
                "-m",
                "pytest",
                "tests/test_project_autonomy_service.py",
                "-q",
                "-k",
                "blast_radius",
            ),
            repo_root,
            timeout_seconds=240,
            capabilities=("blast-radius change control", "unplanned file merge block"),
        ),
        BenchmarkScenario(
            "autopilot-patch-self-review-safety",
            "Autopilot patch self-review safety",
            "quality",
            (
                python,
                "-m",
                "pytest",
                "tests/test_project_autonomy_service.py",
                "-q",
                "-k",
                "patch_self_review",
            ),
            repo_root,
            timeout_seconds=240,
            capabilities=("patch self-review minimality", "high-blast-radius diff rejection"),
        ),
        BenchmarkScenario(
            "autopilot-quality-gates",
            "Autopilot quality gates",
            "quality",
            (
                python,
                "-m",
                "pytest",
                "tests/test_project_autonomy_service.py::test_autopilot_quality_bar_requires_coding_benchmark_scorecard",
                "tests/test_project_autonomy_service.py::test_coding_benchmark_scorecard_can_satisfy_quality_gate",
                "tests/test_project_autonomy_service.py::test_coding_benchmark_signal_treats_repaired_replay_rows_as_clean",
                "tests/test_project_autonomy_service.py::test_agent_os_readiness_operator_inbox_names_goal_receipt_quality",
                "-q",
            ),
            repo_root,
            timeout_seconds=240,
            capabilities=(
                "operator-visible benchmark gate",
                "local coder readiness guardrail",
                "real report replay grading",
                "archived task semantic replay",
            ),
        ),
        BenchmarkScenario(
            "autopilot-frontier-readiness-audit",
            "Autopilot frontier readiness audit",
            "quality",
            (
                python,
                "scripts/autopilot_frontier_readiness_audit.py",
                "--no-write",
                "--json",
            ),
            repo_root,
            timeout_seconds=120,
            capabilities=(
                "frontier readiness gap audit",
                "operator-visible benchmark gate",
                "frontier-agent parity regression gate",
                "current source freshness gate",
            ),
        ),
        BenchmarkScenario(
            "autopilot-frontier-gap-matrix",
            "Autopilot frontier gap matrix",
            "quality",
            (
                python,
                "-m",
                "pytest",
                "tests/test_autopilot_frontier_gap_matrix.py",
                "-q",
            ),
            repo_root,
            timeout_seconds=120,
            capabilities=(
                "frontier gap matrix",
                "frontier readiness gap audit",
                "operator-visible benchmark gate",
                "frontier-agent parity regression gate",
            ),
        ),
        BenchmarkScenario(
            "autopilot-meso-project-workflow-tournament-harness",
            "Autopilot meso project workflow tournament harness",
            "quality",
            (
                python,
                "-m",
                "pytest",
                "tests/test_autopilot_meso_project_workflow_tournament.py",
                "-q",
            ),
            repo_root,
            timeout_seconds=180,
            capabilities=(
                "meso multi-file system tournament",
                "held-out workflow behavior evidence",
                "correctness-first cross-system scoring",
                "premium-independent quality tie-break",
            ),
        ),
        BenchmarkScenario(
            "autopilot-macro-long-horizon-tournament-harness",
            "Autopilot macro long-horizon tournament harness",
            "quality",
            (
                python,
                "-m",
                "pytest",
                "tests/test_project_autonomy_workflow_skills.py",
                "tests/test_autopilot_macro_long_horizon_tournament.py",
                "-q",
            ),
            repo_root,
            timeout_seconds=300,
            capabilities=(
                "macro long-horizon system tournament",
                "three-phase cumulative repository evolution",
                "resumable CHILI-owned workflow skills",
                "hidden cumulative behavior evidence",
                "premium-independent long-horizon execution",
            ),
        ),
        BenchmarkScenario(
            "autopilot-deep-context-reasoning-tournament-harness",
            "Autopilot deep-context reasoning tournament harness",
            "quality",
            (
                python,
                "-m",
                "pytest",
                "tests/test_project_autonomy_context_skills.py",
                "tests/test_autopilot_deep_context_reasoning_tournament.py",
                "-q",
            ),
            repo_root,
            timeout_seconds=300,
            capabilities=(
                "deep-context system tournament",
                "distractor-heavy AST symbol scope resolution",
                "minimal contract-owner selection",
                "held-out deep-context behavior evidence",
                "premium-independent context reasoning",
            ),
        ),
        BenchmarkScenario(
            "autopilot-model-promotion-replay",
            "Autopilot model/tool promotion replay",
            "quality",
            (
                python,
                "scripts/autopilot_model_promotion_benchmark.py",
                "--no-write",
            ),
            repo_root,
            timeout_seconds=120,
            capabilities=(
                "benchmark-driven model promotion",
                "scorecard comparison gate",
                "time-cost promotion budget",
            ),
        ),
        BenchmarkScenario(
            "autopilot-real-chili-regression-replay",
            "Autopilot real CHILI regression replay",
            "quality",
            (
                python,
                "scripts/autopilot_real_chili_regression_benchmark.py",
                "--json",
                "--no-write",
            ),
            repo_root,
            timeout_seconds=240,
            capabilities=(
                "real CHILI regression replay",
                "real bug-slice evidence",
                "expanded real CHILI regression corpus",
                "trading import cycle regression",
                "frontier-agent parity regression gate",
            ),
        ),
        BenchmarkScenario(
            "autopilot-real-chili-candidate-bakeoff",
            "Autopilot real CHILI candidate bakeoff",
            "quality",
            (
                python,
                "scripts/autopilot_real_chili_candidate_bakeoff.py",
                "--no-write",
            ),
            repo_root,
            timeout_seconds=240,
            capabilities=(
                "real CHILI candidate bakeoff",
                "candidate patch outcome comparison",
                "real bug-slice evidence",
                "frontier-agent parity regression gate",
            ),
        ),
        BenchmarkScenario(
            "autopilot-model-candidate-artifact-builder",
            "Autopilot model candidate artifact builder",
            "quality",
            (
                python,
                "scripts/autopilot_model_candidate_artifact_builder.py",
                "--self-test",
                "--no-write",
            ),
            repo_root,
            timeout_seconds=120,
            capabilities=(
                "model output artifact collection",
                "external model artifact intake",
                "real bug-slice evidence",
                "frontier-agent parity regression gate",
            ),
        ),
        BenchmarkScenario(
            "autopilot-model-candidate-prompt-pack",
            "Autopilot model candidate prompt pack",
            "quality",
            (
                python,
                "scripts/autopilot_model_candidate_artifact_builder.py",
                "--emit-prompt-pack",
                "--no-write",
            ),
            repo_root,
            timeout_seconds=120,
            capabilities=(
                "model candidate handoff prompt pack",
                "model output artifact collection",
                "real bug-slice evidence",
                "frontier-agent parity regression gate",
            ),
        ),
        BenchmarkScenario(
            "autopilot-frontier-prompt-pack-bundle",
            "Autopilot frontier prompt-pack bundle",
            "quality",
            (
                python,
                "scripts/autopilot_frontier_prompt_pack_bundle.py",
                "--no-write",
                "--json",
            ),
            repo_root,
            timeout_seconds=120,
            capabilities=(
                "frontier source-specific prompt pack",
                "model candidate handoff prompt pack",
                "frontier model evidence intake orchestration",
                "frontier-agent parity regression gate",
            ),
        ),
        BenchmarkScenario(
            "autopilot-frontier-model-evidence-setup",
            "Autopilot frontier model evidence setup",
            "quality",
            (
                python,
                "-m",
                "pytest",
                "tests/test_autopilot_frontier_model_evidence_setup.py",
                "-q",
            ),
            repo_root,
            timeout_seconds=180,
            capabilities=(
                "frontier model evidence setup safety",
                "frontier source-specific prompt pack",
                "frontier model evidence intake orchestration",
                "model candidate provenance gate",
                "frontier-agent parity regression gate",
            ),
        ),
        BenchmarkScenario(
            "autopilot-frontier-source-collection-packet",
            "Autopilot frontier source collection packet",
            "quality",
            (
                python,
                "-m",
                "pytest",
                "tests/test_autopilot_frontier_source_collection_packet.py",
                "-q",
            ),
            repo_root,
            timeout_seconds=180,
            capabilities=(
                "frontier source collection packet",
                "frontier source-specific prompt pack",
                "frontier source evidence recording",
                "frontier source suite response import",
                "frontier response drop parsing",
                "model candidate handoff prompt pack",
                "frontier model evidence intake orchestration",
                "frontier-agent parity regression gate",
            ),
        ),
        BenchmarkScenario(
            "autopilot-frontier-source-runner",
            "Autopilot frontier source runner",
            "quality",
            (
                python,
                "-m",
                "pytest",
                "tests/test_autopilot_frontier_source_runner.py",
                "tests/test_autopilot_frontier_source_availability_diagnostics.py",
                "-q",
            ),
            repo_root,
            timeout_seconds=180,
            capabilities=(
                "frontier source runner automation",
                "exact Codex 5.6 source collection",
                "exact Fable 5 source collection",
                "measured frontier runtime provenance",
                "frontier source live availability probe",
                "frontier source collection packet",
                "frontier source-specific prompt pack",
                "frontier source evidence recording",
                "frontier source suite response import",
                "frontier response drop parsing",
                "model run drop collector",
                "model candidate provenance gate",
                "frontier model evidence intake orchestration",
                "frontier-agent parity regression gate",
            ),
        ),
        BenchmarkScenario(
            "autopilot-local-model-evidence-recorder",
            "Autopilot local-model evidence recorder",
            "quality",
            (
                python,
                "-m",
                "pytest",
                "tests/test_autopilot_local_model_evidence_recorder.py",
                "-q",
            ),
            repo_root,
            timeout_seconds=180,
            capabilities=(
                "local model evidence recording",
                "frontier model evidence setup safety",
                "model run drop collector",
                "model candidate provenance gate",
                "frontier model evidence intake orchestration",
                "frontier-agent parity regression gate",
            ),
        ),
        BenchmarkScenario(
            "autopilot-frontier-source-evidence-recorder",
            "Autopilot frontier source evidence recorder",
            "quality",
            (
                python,
                "-m",
                "pytest",
                "tests/test_autopilot_frontier_source_evidence_recorder.py",
                "-q",
            ),
            repo_root,
            timeout_seconds=180,
            capabilities=(
                "frontier source evidence recording",
                "frontier response drop parsing",
                "external model artifact intake",
                "model run drop collector",
                "model candidate provenance gate",
                "frontier model evidence intake orchestration",
                "frontier-agent parity regression gate",
            ),
        ),
        BenchmarkScenario(
            "autopilot-frontier-evidence-preflight",
            "Autopilot frontier evidence preflight recovery routing",
            "quality",
            (
                python,
                "-m",
                "pytest",
                "tests/test_autopilot_frontier_evidence_preflight.py",
                "-q",
            ),
            repo_root,
            timeout_seconds=180,
            capabilities=(
                "frontier evidence preflight recovery routing",
                "frontier response drop parsing",
                "frontier source collection packet",
                "frontier source evidence recording",
                "model run drop collector",
                "model candidate provenance gate",
                "frontier-agent parity regression gate",
            ),
        ),
        BenchmarkScenario(
            "autopilot-local-model-candidate-runner",
            "Autopilot local-model candidate runner",
            "quality",
            (
                python,
                "-m",
                "pytest",
                "tests/test_autopilot_local_model_candidate_runner.py",
                "-q",
            ),
            repo_root,
            timeout_seconds=180,
            capabilities=(
                "compact local model candidate collection",
                "local model candidate suite collection",
                "parse-failure repair continuation",
                "early test-derived deterministic repair",
                "measured model-plus-repair latency",
                "local model evidence recording",
                "model run drop collector",
                "model candidate provenance gate",
                "frontier model evidence intake orchestration",
                "frontier-agent parity regression gate",
            ),
        ),
        BenchmarkScenario(
            "autopilot-model-candidate-drop-collector",
            "Autopilot model candidate drop collector",
            "quality",
            (
                python,
                "scripts/autopilot_model_candidate_drop_collector.py",
                "--self-test",
                "--json",
            ),
            repo_root,
            timeout_seconds=120,
            capabilities=(
                "model run drop collector",
                "model candidate provenance gate",
                "model output artifact collection",
                "external model artifact intake",
                "frontier-agent parity regression gate",
            ),
        ),
        BenchmarkScenario(
            "autopilot-model-candidate-provenance",
            "Autopilot model candidate provenance gate",
            "quality",
            (
                python,
                "scripts/autopilot_model_candidate_provenance_benchmark.py",
                "--no-write",
            ),
            repo_root,
            timeout_seconds=120,
            capabilities=(
                "model candidate provenance gate",
                "model output artifact collection",
                "external model artifact intake",
                "frontier-agent parity regression gate",
            ),
        ),
        BenchmarkScenario(
            "autopilot-frontier-model-evidence-intake",
            "Autopilot frontier model evidence intake",
            "quality",
            (
                python,
                "-m",
                "pytest",
                "tests/test_autopilot_frontier_model_evidence_intake.py",
                "-q",
            ),
            repo_root,
            timeout_seconds=240,
            capabilities=(
                "frontier model evidence intake orchestration",
                "model run drop collector",
                "model candidate provenance gate",
                "real model shadow evidence gate",
                "multi-source model candidate tournament",
                "frontier-agent parity regression gate",
            ),
        ),
        BenchmarkScenario(
            "autopilot-model-shadow-evidence",
            "Autopilot model shadow evidence gate",
            "quality",
            (
                python,
                "scripts/autopilot_model_shadow_evidence_benchmark.py",
                "--self-test",
                "--no-write",
            ),
            repo_root,
            timeout_seconds=120,
            capabilities=(
                "real model shadow evidence gate",
                "model run drop collector",
                "model candidate provenance gate",
                "external model artifact intake",
                "frontier-agent parity regression gate",
            ),
        ),
        BenchmarkScenario(
            "autopilot-model-shadow-evidence-mode-gate",
            "Autopilot model shadow evidence mode gate",
            "quality",
            (
                python,
                "-m",
                "pytest",
                "tests/test_project_autonomy_service.py::test_coding_benchmark_scorecard_rejects_model_shadow_self_test_mode",
                "tests/test_autopilot_model_shadow_evidence_benchmark.py::test_shadow_evidence_cli_real_manifest_writes_promotion_ready_scorecard",
                "-q",
            ),
            repo_root,
            timeout_seconds=120,
            capabilities=(
                "real model shadow evidence gate",
                "real shadow evidence mode gate",
                "frontier-agent parity regression gate",
            ),
        ),
        BenchmarkScenario(
            "autopilot-model-candidate-artifact-bakeoff",
            "Autopilot model candidate artifact bakeoff",
            "quality",
            (
                python,
                "scripts/autopilot_model_candidate_artifact_bakeoff.py",
                "--no-write",
            ),
            repo_root,
            timeout_seconds=240,
            capabilities=(
                "external model artifact intake",
                "model-output patch replay",
                "candidate patch outcome comparison",
                "frontier-agent parity regression gate",
            ),
        ),
        BenchmarkScenario(
            "autopilot-model-candidate-tournament",
            "Autopilot model candidate tournament",
            "quality",
            (
                python,
                "scripts/autopilot_model_candidate_tournament_benchmark.py",
                "--no-write",
            ),
            repo_root,
            timeout_seconds=240,
            capabilities=(
                "multi-source model candidate tournament",
                "candidate patch outcome comparison",
                "real bug-slice evidence",
                "frontier-agent parity regression gate",
            ),
        ),
        BenchmarkScenario(
            "autopilot-model-candidate-tournament-evidence-mode-gate",
            "Autopilot model candidate tournament evidence mode gate",
            "quality",
            (
                python,
                "-m",
                "pytest",
                "tests/test_autopilot_coding_benchmark.py::test_benchmark_scorecard_requires_real_model_tournament_mode",
                "tests/test_autopilot_model_candidate_tournament_benchmark.py::test_tournament_drop_dir_can_require_candidate_provenance",
                "-q",
            ),
            repo_root,
            timeout_seconds=120,
            capabilities=(
                "multi-source model candidate tournament",
                "real model tournament evidence mode gate",
                "frontier-agent parity regression gate",
            ),
        ),
        BenchmarkScenario(
            "autopilot-frontier-bakeoff-replay",
            "Autopilot frontier patch bakeoff replay",
            "quality",
            (
                python,
                "scripts/autopilot_frontier_bakeoff_benchmark.py",
                "--no-write",
            ),
            repo_root,
            timeout_seconds=180,
            capabilities=(
                "frontier patch bakeoff grading",
                "candidate patch outcome comparison",
                "frontier-agent parity regression gate",
            ),
        ),
        BenchmarkScenario(
            "agentops-report-replay-quality",
            "AgentOps report replay quality",
            "quality",
            (
                python,
                "scripts/autopilot_report_replay_benchmark.py",
                "--min-score",
                "85",
                "--min-reports",
                "3",
                "--min-age-seconds",
                "600",
                "--no-write",
            ),
            repo_root,
            timeout_seconds=120,
            capabilities=("real report replay grading", "agent report receipt completeness"),
        ),
        BenchmarkScenario(
            "autopilot-task-replay-quality",
            "Autopilot task replay quality",
            "quality",
            (
                python,
                "scripts/autopilot_task_replay_benchmark.py",
                "--no-write",
            ),
            repo_root,
            timeout_seconds=120,
            capabilities=("task-level replay grading", "senior-engineer gate decisions"),
        ),
        BenchmarkScenario(
            "agentops-archived-task-replay-quality",
            "AgentOps archived task semantic replay quality",
            "quality",
            (
                python,
                "scripts/autopilot_archived_task_replay_benchmark.py",
                "--min-score",
                "85",
                "--min-reports",
                "4",
                "--min-age-seconds",
                "600",
                "--no-write",
            ),
            repo_root,
            timeout_seconds=120,
            capabilities=("archived task semantic replay", "real artifact decision grading"),
        ),
        BenchmarkScenario(
            "autopilot-recovery-safety",
            "Autopilot recovery safety",
            "recovery",
            (
                python,
                "-m",
                "pytest",
                "tests/test_project_autonomy_service.py::test_validation_policy_blocker_reaches_global_readiness_actions",
                "tests/test_project_autonomy_service.py::test_runtime_control_blocked_run_guides_review_instead_of_rerun",
                "tests/test_project_autonomy_service.py::test_manual_merge_fails_closed_for_non_completed_runs",
                "-q",
            ),
            repo_root,
            timeout_seconds=240,
            capabilities=("failed-closed merge control", "runtime-control recovery guidance"),
        ),
        BenchmarkScenario(
            "autopilot-patch-application-safety",
            "Autopilot patch application safety",
            "patch",
            (
                python,
                "-m",
                "pytest",
                "tests/test_project_autonomy_service.py",
                "-q",
                "-k",
                "generate_diffs_reports_rejected_model_output or small_desktop_fallback_diff_handles_bad_model_patch or git_apply_check_accepts_stdin_patch_on_windows",
            ),
            repo_root,
            timeout_seconds=240,
            capabilities=("model patch rejection evidence", "git apply check compatibility"),
        ),
    ]
    mobile_scenarios = [
        BenchmarkScenario(
            "mobile-quality-action",
            "Mobile quality action routing",
            "operator-ui",
            flutter
            + (
                "test",
                "test/autopilot_quality_action_presenter_test.dart",
            ),
            mobile,
            timeout_seconds=240,
            capabilities=("mobile quality action routing",),
        ),
        BenchmarkScenario(
            "mobile-agent-bench-presenter",
            "Mobile agent bench presenter",
            "operator-ui",
            flutter
            + (
                "test",
                "test/autopilot_agent_bench_activity_presenter_test.dart",
            ),
            mobile,
            timeout_seconds=300,
            capabilities=("mobile benchmark activity presentation",),
        ),
        BenchmarkScenario(
            "mobile-brain-analysis",
            "Mobile Brain quality analysis",
            "operator-ui",
            flutter
            + (
                "analyze",
                "lib/src/brain/autopilot_quality_action_presenter.dart",
                "lib/src/brain/autopilot_agent_bench_activity_presenter.dart",
                "lib/src/brain/brain_dispatch_screen.dart",
            ),
            mobile,
            timeout_seconds=240,
            capabilities=("mobile static analysis",),
        ),
    ]
    if include_mobile:
        scenarios.extend(mobile_scenarios)
    return [_guarded_pytest_scenario(scenario) for scenario in scenarios]


def _tool_command(tool: str) -> tuple[str, ...]:
    resolved = shutil.which(tool)
    if not resolved:
        return (tool,)
    if os.name == "nt" and resolved.lower().endswith((".bat", ".cmd")):
        return ("cmd.exe", "/d", "/c", resolved)
    return (resolved,)


def _command_text(command: Sequence[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(list(command))
    return shlex.join(command)


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _clip_evidence_line(line: str, limit: int = OUTPUT_LINE_SNIPPET_CHARS) -> str:
    text = str(line or "").strip()
    if len(text) <= limit:
        return text
    head = max(80, (limit * 2) // 3)
    tail = max(40, limit - head - 5)
    return text[:head].rstrip() + " ... " + text[-tail:].lstrip()


def _is_nested_failure_line(line: str) -> bool:
    if "|" not in line:
        return False
    cells = [cell.strip().lower() for cell in line.split("|") if cell.strip()]
    return any(
        cell in {"failed", "timed_out", "error", "blocked"}
        or cell.startswith("failed/")
        for cell in cells
    )


def _json_summary_identity(row: object) -> str:
    if not isinstance(row, dict):
        return str(row or "").strip()
    for key in ("case_id", "check_id", "scenario_id", "id", "name"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("regression_class", "decision_class", "comparison_class", "source_kind"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unnamed"


def _json_summary_status(row: object) -> str:
    if not isinstance(row, dict):
        return ""
    for key in ("status", "result", "actual_status", "decision", "winner"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    score = row.get("score")
    if isinstance(score, (int, float)) and score < 100:
        return f"score={score:g}"
    return ""


def _json_summary_evidence(row: object) -> str:
    if not isinstance(row, dict):
        return ""
    for key in ("evidence", "reason", "error", "detail"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _json_row_failed(row: object) -> bool:
    if not isinstance(row, dict):
        return False
    expected = str(row.get("expected_status") or row.get("expected") or "").strip().lower()
    actual = str(row.get("actual_status") or "").strip().lower()
    if expected and actual and expected != actual:
        return True
    status = str(row.get("status") or row.get("result") or "").strip().lower()
    if status and status not in {"passed", "pass", "accepted", "ok", "success", "successful"}:
        return True
    score = row.get("score")
    return isinstance(score, (int, float)) and score < 100


def _json_result_rows(payload: object) -> list[object]:
    if not isinstance(payload, dict):
        return []
    rows: list[object] = []
    for key in ("results", "checks", "cases", "comparisons"):
        value = payload.get(key)
        if isinstance(value, list):
            rows.extend(value)
    return rows


def _compact_json_output(text: str) -> str | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    summary_parts: list[str] = []
    for key in (
        "schema",
        "status",
        "average_score",
        "score",
        "readiness_score",
        "cases",
        "checks",
        "scenarios",
        "requirements",
        "blockers",
        "transcript_events",
        "validated_with_provenance",
    ):
        value = payload.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            summary_parts.append(f"{key}={value}")
    rows = _json_result_rows(payload)
    failed_rows = [row for row in rows if _json_row_failed(row)]
    if failed_rows:
        summary_parts.append(f"failures={len(failed_rows)}")
        for row in failed_rows[:3]:
            identity = _json_summary_identity(row)
            status = _json_summary_status(row)
            evidence = _json_summary_evidence(row)
            failure = ": ".join(part for part in (identity, status, evidence) if part)
            if failure:
                summary_parts.append(failure)
    elif rows:
        summary_parts.append(f"rows={len(rows)}")
    if not summary_parts:
        return None
    return _clip_evidence_line("; ".join(summary_parts), limit=OUTPUT_SNIPPET_CHARS)


def _compact_output(stdout: object, stderr: object) -> str:
    combined = "\n".join(part for part in (_as_text(stdout), _as_text(stderr)) if part).strip()
    if not combined:
        return "Command completed without output."
    json_summary = _compact_json_output(combined)
    if json_summary:
        return json_summary
    lines = [line.strip() for line in combined.splitlines() if line.strip()]
    if not lines:
        return "Command completed without output."
    markers = (" failed", "failure", "error", "assertionerror", "e   ", "[winerror")
    nested_failures = [
        line
        for line in lines
        if _is_nested_failure_line(line)
    ][:3]
    interesting = [
        line
        for line in lines
        if any(marker in line.lower() for marker in markers)
    ][:4]
    selected: list[str] = []
    for line in [*nested_failures, *interesting, *lines[-6:]]:
        clipped = _clip_evidence_line(line)
        if clipped not in selected:
            selected.append(clipped)
    text = " | ".join(selected)
    if len(text) > OUTPUT_SNIPPET_CHARS:
        return text[: OUTPUT_SNIPPET_CHARS - 3].rstrip() + "..."
    return text


def _environment_failure_reason(exit_code: int | None, evidence: str) -> str | None:
    normalized = " ".join(evidence.lower().split())
    if exit_code == 79:
        return "pytest runtime is outside the repo-supported range"
    if "pytest runtime unsupported" in normalized:
        return "pytest runtime is outside the repo-supported range"
    if exit_code in {-1, 4294967295}:
        return "process transport returned an interruption code before useful output was captured"
    if exit_code in {-9, 137}:
        return "process was killed, which usually points to host resource pressure or an external stop"
    lock_markers = (
        "flutter.bat.lock",
        "flutter tool lock",
        "waiting for another flutter command",
        "unable to acquire lock",
        "lockfile",
        "being used by another process",
        "[winerror 32]",
        "resource temporarily unavailable",
    )
    if any(marker in normalized for marker in lock_markers):
        return "toolchain lock or file-handle contention blocked the scenario"
    missing_dependency_markers = (
        "modulenotfounderror",
        "no module named",
        "importerror while importing test module",
        "error collecting tests/",
    )
    if any(marker in normalized for marker in missing_dependency_markers):
        return "pytest runtime is missing a project dependency"
    runner_markers = (
        "repo-local python environment",
        "nativecommanderror",
        "exit137",
        "exit 137",
        "killed",
        "terminated due to signal",
    )
    if any(marker in normalized for marker in runner_markers):
        return "runner or host interrupted the child process"
    if exit_code not in {None, 0} and evidence == "Command completed without output.":
        return "command exited non-zero without diagnostic output, so the runner lost the useful failure signal"
    return None


def _environment_failure_evidence(exit_code: int | None, evidence: str) -> str | None:
    reason = _environment_failure_reason(exit_code, evidence)
    if not reason:
        return None
    return (
        f"Runner/environment issue: {reason}. "
        f"Recovery: {ENVIRONMENT_RECOVERY_GUIDANCE}. "
        f"Raw evidence: {evidence}"
    )


def _version_parts(raw: str) -> tuple[int, int, int]:
    parts: list[int] = []
    for chunk in raw.split("."):
        digits = ""
        for char in chunk:
            if not char.isdigit():
                break
            digits += char
        parts.append(int(digits or "0"))
        if len(parts) == 3:
            break
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


def _installed_pytest_version() -> str:
    try:
        return importlib.metadata.version("pytest")
    except importlib.metadata.PackageNotFoundError:
        return "missing"


def _pytest_version_supported(version: str) -> bool:
    if version == "missing":
        return False
    major, minor, _patch = _version_parts(version)
    return major == 8 and minor >= 2


def _scenario_uses_pytest_module(scenario: BenchmarkScenario) -> bool:
    command = list(scenario.command)
    if any(
        command[index] == "-m" and index + 1 < len(command) and command[index + 1] == "pytest"
        for index in range(len(command) - 1)
    ):
        return True
    return any(
        Path(token).name == "pytest_adaptive.py"
        and index + 1 < len(command)
        and command[index + 1] == "run"
        for index, token in enumerate(command)
    )


def _scenario_uses_pytest_adaptive(scenario: BenchmarkScenario) -> bool:
    command = list(scenario.command)
    return any(
        Path(token).name == "pytest_adaptive.py"
        and index + 1 < len(command)
        and command[index + 1] == "run"
        for index, token in enumerate(command)
    )


def _scenario_environment_preflight(scenario: BenchmarkScenario) -> str | None:
    if not _scenario_uses_pytest_module(scenario):
        return None
    if _scenario_uses_pytest_adaptive(scenario):
        try:
            from scripts import pytest_adaptive

            contract = pytest_adaptive.pytest_runtime_contract(REPO_ROOT)
        except Exception as exc:
            return (
                "Runner/environment issue: pytest adaptive runtime probe failed. "
                f"Recovery: create or select a repo-local Python environment with {PYTEST_SUPPORTED_SPEC} "
                f"before rerunning this scenario. Raw evidence: {type(exc).__name__}: {exc}"
            )
        if contract.passed:
            return None
        return (
            "Runner/environment issue: pytest-backed scenario requires "
            f"{contract.required}, but selected pytest is {contract.actual} "
            f"from {contract.source} ({contract.python}). "
            f"Recovery: {contract.recovery}."
        )
    version = _installed_pytest_version()
    if _pytest_version_supported(version):
        return None
    return (
        "Runner/environment issue: pytest-backed scenario requires "
        f"{PYTEST_SUPPORTED_SPEC}, but installed pytest is {version}. "
        "Recovery: install the repo-supported test runner "
        f"(`python -m pip install \"{PYTEST_SUPPORTED_SPEC}\"`) before rerunning this scenario. "
        "Raw evidence: requirements.txt pins pytest below 9 because pytest 9 requires the "
        "pytest-asyncio 1.x migration to be evaluated first."
    )


def _heartbeat_seconds() -> float:
    raw = os.environ.get("CHILI_CODING_BENCHMARK_HEARTBEAT_SECONDS", "")
    if not raw.strip():
        return DEFAULT_HEARTBEAT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_HEARTBEAT_SECONDS
    return max(0.01, value)


def _read_process_output_file(path: Path) -> str:
    for attempt in range(5):
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            if attempt == 4:
                return ""
            time.sleep(0.1)
    return ""


def _cleanup_process_output_dir(path: Path) -> None:
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def _emit_benchmark_progress(message: str) -> None:
    stream_name = os.environ.get("CHILI_CODING_BENCHMARK_PROGRESS_STREAM", "stderr")
    key = stream_name.strip().lower()
    if key in {"0", "false", "off", "none", "quiet"}:
        return
    stream = sys.stdout if key == "stdout" else sys.stderr
    print(message, file=stream, flush=True)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def read_source_quiet_lease(
    lease_path: Path = DEFAULT_SOURCE_QUIET_LEASE_PATH,
) -> dict[str, object]:
    if not lease_path.is_file():
        return {}
    try:
        data = json.loads(lease_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def active_source_quiet_lease(
    lease_path: Path = DEFAULT_SOURCE_QUIET_LEASE_PATH,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    lease = read_source_quiet_lease(lease_path)
    if str(lease.get("status") or "").lower() != "active":
        return {}
    expires_at = _parse_utc(lease.get("expires_utc"))
    if expires_at is None:
        return lease
    if expires_at <= (now or _utc_now()):
        return {}
    return lease


def source_quiet_write_blocker(
    lease_path: Path = DEFAULT_SOURCE_QUIET_LEASE_PATH,
    *,
    now: datetime | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Return a fail-closed message when source/test edits should pause."""
    active = active_source_quiet_lease(lease_path, now=now)
    if not active:
        return ""
    lease_id = str(active.get("lease_id") or "").strip()
    env = environ if environ is not None else os.environ
    current_lease_id = str(env.get(SOURCE_QUIET_LEASE_ENV, "")).strip()
    if lease_id and current_lease_id == lease_id:
        return ""
    holder = str(active.get("holder") or "unknown").strip()
    expires = str(active.get("expires_utc") or "unknown").strip()
    boundary = str(active.get("permission_boundary") or "").strip()
    message = (
        "Source quiet benchmark lease is active; pause source/test edits until the "
        f"lease is released. lease_id={lease_id or 'unknown'} holder={holder} "
        f"expires_utc={expires}"
    )
    if boundary:
        message += f"; boundary={boundary}"
    return message


def acquire_source_quiet_lease(
    *,
    lease_path: Path = DEFAULT_SOURCE_QUIET_LEASE_PATH,
    holder: str,
    quiet_seconds: float,
    lease_seconds: float,
    scenarios: Sequence[BenchmarkScenario],
    now: datetime | None = None,
) -> dict[str, object]:
    now = now or _utc_now()
    active = active_source_quiet_lease(lease_path, now=now)
    current_lease_id = os.environ.get(SOURCE_QUIET_LEASE_ENV, "").strip()
    if active and active.get("lease_id") != current_lease_id:
        raise RuntimeError(
            "Source quiet benchmark lease is already active: "
            f"{active.get('lease_id') or 'unknown'}"
        )
    lease_id = uuid.uuid4().hex
    lease_seconds = max(float(lease_seconds), 1.0)
    lease = {
        "schema": SOURCE_QUIET_LEASE_SCHEMA_VERSION,
        "lease_id": lease_id,
        "status": "active",
        "holder": holder,
        "pid": os.getpid(),
        "created_utc": _format_utc(now),
        "expires_utc": _format_utc(now + timedelta(seconds=lease_seconds)),
        "quiet_seconds": max(float(quiet_seconds), 0.0),
        "lease_seconds": lease_seconds,
        "scenario_count": len(scenarios),
        "scenario_ids": [scenario.scenario_id for scenario in scenarios],
        "permission_boundary": (
            "Benchmark source quiet lease only; blocks unrelated autonomous source/test "
            "edits while promotion evidence is being collected."
        ),
    }
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    lease_path.write_text(json.dumps(lease, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.environ[SOURCE_QUIET_LEASE_ENV] = lease_id
    return lease


def release_source_quiet_lease(
    lease: Mapping[str, object] | None,
    *,
    lease_path: Path = DEFAULT_SOURCE_QUIET_LEASE_PATH,
    now: datetime | None = None,
) -> None:
    if not lease:
        return
    current = read_source_quiet_lease(lease_path)
    if current.get("lease_id") != lease.get("lease_id"):
        return
    current["status"] = "released"
    current["released_utc"] = _format_utc(now or _utc_now())
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    lease_path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if os.environ.get(SOURCE_QUIET_LEASE_ENV) == lease.get("lease_id"):
        os.environ.pop(SOURCE_QUIET_LEASE_ENV, None)


def _popen_command(command: Sequence[str]) -> Sequence[str] | str:
    command_list = list(command)
    if (
        os.name == "nt"
        and len(command_list) >= 4
        and Path(command_list[0]).name.lower() == "cmd.exe"
        and command_list[1].lower() == "/d"
        and command_list[2].lower() == "/c"
        and Path(command_list[3]).suffix.lower() in {".bat", ".cmd"}
    ):
        prefix = subprocess.list2cmdline(command_list[:3])
        target = command_list[3].strip('"')
        rest = subprocess.list2cmdline(command_list[4:])
        return f'{prefix} "{target}"' + (f" {rest}" if rest else "")
    return command_list


def _print_scenario_heartbeat(
    scenario: BenchmarkScenario,
    started: float,
    process: subprocess.Popen[str],
) -> None:
    elapsed = time.monotonic() - started
    elapsed_label = f"{elapsed:.1f}s" if elapsed < 10 else f"{elapsed:.0f}s"
    timeout = scenario.timeout_seconds
    _emit_benchmark_progress(
        "[coding-benchmark] "
        f"{scenario.scenario_id} still running "
        f"elapsed={elapsed_label} timeout={timeout}s pid={process.pid}",
    )


def _retry_timeout_seconds(timeout_seconds: int) -> int:
    return min(
        max(int(timeout_seconds) * TIMEOUT_RETRY_MULTIPLIER, int(timeout_seconds) + 60),
        TIMEOUT_RETRY_MAX_SECONDS,
    )


def _run_scenario_once(scenario: BenchmarkScenario) -> BenchmarkResult:
    started = time.monotonic()
    if not scenario.cwd.exists():
        return BenchmarkResult(
            scenario=scenario,
            status="failed",
            exit_code=None,
            duration_seconds=0.0,
            evidence=f"Working directory does not exist: {scenario.cwd}",
        )
    environment_preflight = _scenario_environment_preflight(scenario)
    if environment_preflight:
        return BenchmarkResult(
            scenario=scenario,
            status=ENVIRONMENT_BLOCKED_STATUS,
            exit_code=None,
            duration_seconds=time.monotonic() - started,
            evidence=environment_preflight,
        )
    process: subprocess.Popen[str] | None = None
    stdout = ""
    stderr = ""
    timed_out = False
    tmp_dir: Path | None = None
    try:
        tmp_dir = Path(tempfile.mkdtemp(prefix="chili_coding_benchmark_"))
        stdout_path = tmp_dir / "stdout.txt"
        stderr_path = tmp_dir / "stderr.txt"
        with stdout_path.open("w", encoding="utf-8", errors="replace") as stdout_handle:
            with stderr_path.open("w", encoding="utf-8", errors="replace") as stderr_handle:
                process = subprocess.Popen(
                    _popen_command(scenario.command),
                    cwd=scenario.cwd,
                    text=True,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                )
                heartbeat = _heartbeat_seconds()
                while True:
                    elapsed = time.monotonic() - started
                    remaining = scenario.timeout_seconds - elapsed
                    if remaining <= 0:
                        process.kill()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            pass
                        timed_out = True
                        break
                    try:
                        process.wait(timeout=min(heartbeat, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        _print_scenario_heartbeat(scenario, started, process)
        stdout = _read_process_output_file(stdout_path)
        stderr = _read_process_output_file(stderr_path)
    except OSError as exc:
        duration = time.monotonic() - started
        evidence = str(exc)
        environment_evidence = _environment_failure_evidence(None, evidence)
        return BenchmarkResult(
            scenario=scenario,
            status=ENVIRONMENT_BLOCKED_STATUS if environment_evidence else "failed",
            exit_code=None,
            duration_seconds=duration,
            evidence=environment_evidence or evidence,
        )
    finally:
        if tmp_dir is not None:
            _cleanup_process_output_dir(tmp_dir)
    duration = time.monotonic() - started
    if timed_out:
        evidence = _compact_output(stdout, stderr)
        if evidence == "Command completed without output.":
            evidence = f"Timed out after {scenario.timeout_seconds}s."
        return BenchmarkResult(
            scenario=scenario,
            status="timed_out",
            exit_code=None,
            duration_seconds=duration,
            evidence=evidence,
        )
    exit_code = process.returncode if process is not None else None
    evidence = _compact_output(stdout, stderr)
    environment_evidence = (
        None if exit_code == 0 else _environment_failure_evidence(exit_code, evidence)
    )
    return BenchmarkResult(
        scenario=scenario,
        status=(
            "passed"
            if exit_code == 0
            else ENVIRONMENT_BLOCKED_STATUS
            if environment_evidence
            else "failed"
        ),
        exit_code=exit_code,
        duration_seconds=duration,
        evidence=environment_evidence or evidence,
    )


def run_scenario(scenario: BenchmarkScenario) -> BenchmarkResult:
    first = _run_scenario_once(scenario)
    if first.status != "timed_out":
        return first

    retry_timeout = _retry_timeout_seconds(scenario.timeout_seconds)
    retry_scenario = dataclasses.replace(scenario, timeout_seconds=retry_timeout)
    _emit_benchmark_progress(
        "[coding-benchmark] "
        f"{scenario.scenario_id} retrying after timeout "
        f"retry_timeout={retry_timeout}s",
    )
    retry = _run_scenario_once(retry_scenario)
    evidence = (
        f"First attempt timed out after {scenario.timeout_seconds}s; "
        f"retry timeout={retry_timeout}s status={retry.status}. "
        f"Retry evidence: {retry.evidence}"
    )
    return BenchmarkResult(
        scenario=scenario,
        status=retry.status,
        exit_code=retry.exit_code,
        duration_seconds=first.duration_seconds + retry.duration_seconds,
        evidence=evidence,
    )


def benchmark_score(results: Sequence[BenchmarkResult]) -> int:
    if not results:
        return 0
    return round((sum(1 for result in results if result.passed) / len(results)) * 100)


def _missing_required_capabilities(results: Sequence[BenchmarkResult]) -> list[str]:
    covered = {capability.lower() for capability in _covered_capabilities(results)}
    return [
        capability
        for capability in REQUIRED_CAPABILITIES
        if capability.lower() not in covered
    ]


def benchmark_status(
    results: Sequence[BenchmarkResult],
    target_score: int = TARGET_SCORE,
    minimum_scenarios: int = MIN_SCENARIOS,
    source_stability: dict[str, object] | None = None,
) -> str:
    score = benchmark_score(results)
    source_changed = False
    if source_stability is not None:
        source_changed = str(source_stability.get("status") or "").lower() != "stable"
    if (
        len(results) >= minimum_scenarios
        and score >= target_score
        and all(result.passed for result in results)
        and not _missing_required_capabilities(results)
        and not source_changed
    ):
        return "passed"
    return "failed"


def selected_scenarios_status(results: Sequence[BenchmarkResult]) -> str:
    if results and all(result.passed for result in results):
        return "passed"
    return "failed"


def _escape_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _covered_capabilities(results: Sequence[BenchmarkResult]) -> list[str]:
    capabilities: set[str] = set()
    for result in results:
        capabilities.update(result.scenario.capabilities)
    return sorted(capabilities)


def _source_tree_snapshot(repo_root: Path = REPO_ROOT) -> dict[str, tuple[int, int]]:
    snapshot: dict[str, tuple[int, int]] = {}
    for root_rel in SOURCE_STABILITY_ROOTS:
        root = repo_root / Path(root_rel)
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                name
                for name in dirnames
                if name not in SOURCE_STABILITY_SKIP_DIRS and not name.startswith(".")
            ]
            for filename in filenames:
                if not filename.endswith(SOURCE_STABILITY_SUFFIXES):
                    continue
                path = Path(dirpath) / filename
                try:
                    stat = path.stat()
                    rel_path = path.relative_to(repo_root).as_posix()
                except OSError:
                    continue
                except ValueError:
                    rel_path = str(path)
                snapshot[rel_path] = (int(stat.st_mtime_ns), int(stat.st_size))
    return snapshot


def source_tree_stability(
    before: dict[str, tuple[int, int]],
    after: dict[str, tuple[int, int]],
) -> dict[str, object]:
    changed = sorted(
        path
        for path in set(before) | set(after)
        if before.get(path) != after.get(path)
    )
    return {
        "status": "stable" if not changed else "changed",
        "scanned_files": len(after),
        "changed_count": len(changed),
        "changed_preview": changed[:SOURCE_STABILITY_PREVIEW_LIMIT],
        "changed_files": changed,
    }


def wait_for_source_quiet(
    repo_root: Path = REPO_ROOT,
    *,
    quiet_seconds: float,
    timeout_seconds: float = DEFAULT_SOURCE_QUIET_TIMEOUT_SECONDS,
    poll_seconds: float = DEFAULT_SOURCE_QUIET_POLL_SECONDS,
    snapshot_fn=_source_tree_snapshot,
    sleep_fn=time.sleep,
    now_fn=time.monotonic,
) -> dict[str, object]:
    quiet_seconds = max(float(quiet_seconds), 0.0)
    timeout_seconds = max(float(timeout_seconds), 0.0)
    poll_seconds = max(float(poll_seconds), 0.01)
    if quiet_seconds <= 0:
        snapshot = snapshot_fn(repo_root)
        return {
            "status": "not_required",
            "quiet_seconds": quiet_seconds,
            "timeout_seconds": timeout_seconds,
            "observed_quiet_seconds": 0.0,
            "scanned_files": len(snapshot),
            "changed_count": 0,
            "changed_preview": [],
            "changed_files": [],
        }

    previous = snapshot_fn(repo_root)
    started_at = now_fn()
    stable_since = started_at
    deadline = started_at + timeout_seconds
    last_stability = {
        "status": "stable",
        "scanned_files": len(previous),
        "changed_count": 0,
        "changed_preview": [],
        "changed_files": [],
    }
    while now_fn() <= deadline:
        remaining = deadline - now_fn()
        if remaining <= 0:
            break
        sleep_fn(min(poll_seconds, remaining))
        current = snapshot_fn(repo_root)
        stability = source_tree_stability(previous, current)
        current_time = now_fn()
        if stability["status"] == "stable":
            observed = current_time - stable_since
            if observed >= quiet_seconds:
                return {
                    **stability,
                    "status": "stable",
                    "quiet_seconds": quiet_seconds,
                    "timeout_seconds": timeout_seconds,
                    "observed_quiet_seconds": round(observed, 3),
                }
        else:
            stable_since = current_time
            last_stability = stability
        previous = current

    return {
        **last_stability,
        "status": "timed_out",
        "quiet_seconds": quiet_seconds,
        "timeout_seconds": timeout_seconds,
        "observed_quiet_seconds": round(max(now_fn() - stable_since, 0.0), 3),
    }


def render_scorecard(
    results: Sequence[BenchmarkResult],
    *,
    generated_at: datetime | None = None,
    target_score: int = TARGET_SCORE,
    minimum_scenarios: int = MIN_SCENARIOS,
    profile: str = "core",
    source_stability: dict[str, object] | None = None,
) -> str:
    generated_at = generated_at or datetime.now(timezone.utc)
    total = len(results)
    passed = sum(1 for result in results if result.passed)
    environment_issues = [
        result for result in results if result.status == ENVIRONMENT_BLOCKED_STATUS
    ]
    score = benchmark_score(results)
    status = benchmark_status(
        results,
        target_score=target_score,
        minimum_scenarios=minimum_scenarios,
        source_stability=source_stability,
    )
    duration = sum(result.duration_seconds for result in results)
    source_status = str((source_stability or {}).get("status") or "")
    source_changed_count = int((source_stability or {}).get("changed_count") or 0)
    source_scanned_files = int((source_stability or {}).get("scanned_files") or 0)
    source_preview = [
        str(item)
        for item in ((source_stability or {}).get("changed_preview") or [])
        if str(item).strip()
    ]
    source_changed_files = [
        str(item)
        for item in ((source_stability or {}).get("changed_files") or source_preview)
        if str(item).strip()
    ]
    lines = [
        "# CHILI Coding Benchmark Scorecard",
        "",
        f"- Schema: {BENCHMARK_SCHEMA_VERSION}",
        f"- Profile: {profile}",
        f"- Generated UTC: {generated_at.isoformat().replace('+00:00', 'Z')}",
        f"- Status: {status}",
        f"- Selected scenarios status: {selected_scenarios_status(results)}",
        f"- Target score: {target_score}",
        f"- Minimum scenarios: {minimum_scenarios}",
        f"- Overall score: {score}/100",
        f"- Scenarios: {total}",
        f"- Pass rate: {passed}/{total}",
        f"- Runner/environment issues: {len(environment_issues)}",
        *(
            [f"- Runner/environment recovery: {ENVIRONMENT_RECOVERY_GUIDANCE}"]
            if environment_issues
            else []
        ),
        f"- Duration seconds: {duration:.2f}",
        *(
            [
                f"- Source stability: {source_status}",
                f"- Source files scanned: {source_scanned_files}",
                f"- Source changes during run: {source_changed_count}",
                f"- Source change preview: {', '.join(source_preview) if source_preview else 'none'}",
            ]
            if source_stability is not None
            else []
        ),
        f"- Required capabilities: {', '.join(REQUIRED_CAPABILITIES)}",
        f"- Capability coverage: {', '.join(_covered_capabilities(results)) or 'none'}",
        "- Runner: scripts/autopilot_coding_benchmark.py",
        "- Safety: read-only tests and analyzers only; no git, deployment, runtime restart, broker, migration, or live-trading action.",
        "",
        "| Scenario ID | Scenario | Category | Capability | Result | Seconds | Command | Evidence |",
        "| --- | --- | --- | --- | --- | ---: | --- | --- |",
    ]
    for result in results:
        lines.append(
            "| "
            + " | ".join(
                [
                    _escape_cell(result.scenario.scenario_id),
                    _escape_cell(result.scenario.name),
                    _escape_cell(result.scenario.category),
                    _escape_cell(", ".join(result.scenario.capabilities) or result.scenario.category),
                    _escape_cell(result.status),
                    f"{result.duration_seconds:.2f}",
                    _escape_cell(_command_text(result.scenario.command)),
                    _escape_cell(result.evidence),
                ]
            )
            + " |"
        )
    if source_stability is not None:
        lines.extend(
            [
                "",
                "## Files Changed During Run",
                "",
                "| Path |",
                "| --- |",
            ]
        )
        if source_changed_files:
            for path in source_changed_files:
                lines.append(f"| {_escape_cell(path)} |")
        else:
            lines.append("| none |")
    lines.append("")
    return "\n".join(lines)


def select_scenarios(
    scenarios: Iterable[BenchmarkScenario],
    requested_ids: Sequence[str],
) -> list[BenchmarkScenario]:
    scenario_list = list(scenarios)
    if not requested_ids:
        return scenario_list
    by_id = {scenario.scenario_id: scenario for scenario in scenario_list}
    missing = [scenario_id for scenario_id in requested_ids if scenario_id not in by_id]
    if missing:
        raise SystemExit(f"Unknown benchmark scenario(s): {', '.join(missing)}")
    return [by_id[scenario_id] for scenario_id in requested_ids]


def write_scorecard(markdown: str, output_path: Path = DEFAULT_OUTPUT) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    return output_path


def run_benchmark(
    scenarios: Sequence[BenchmarkScenario],
    *,
    output_path: Path = DEFAULT_OUTPUT,
    write: bool = True,
    profile: str = "core",
) -> tuple[list[BenchmarkResult], str, Path]:
    source_before = _source_tree_snapshot(REPO_ROOT)
    results: list[BenchmarkResult] = []
    for index, scenario in enumerate(scenarios, start=1):
        _emit_benchmark_progress(
            f"[coding-benchmark] {index}/{len(scenarios)} {scenario.scenario_id} start",
        )
        result = run_scenario(scenario)
        results.append(result)
        _emit_benchmark_progress(
            f"[coding-benchmark] {index}/{len(scenarios)} {scenario.scenario_id} {result.status}",
        )
    source_stability = source_tree_stability(
        source_before,
        _source_tree_snapshot(REPO_ROOT),
    )
    markdown = render_scorecard(
        results,
        profile=profile,
        source_stability=source_stability,
    )
    if write:
        write_scorecard(markdown, output_path)
    return results, markdown, output_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run CHILI's local coding benchmark gate.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--scenario", action="append", default=[], help="Run one scenario id; may repeat.")
    parser.add_argument("--include-mobile", action="store_true", help="Add Flutter mobile checks to the core benchmark.")
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="For custom scenario subsets, return success when selected commands pass even if the promotion gate is incomplete.",
    )
    parser.add_argument("--list", action="store_true", help="List scenario ids without running commands.")
    parser.add_argument("--no-write", action="store_true", help="Print the scorecard without writing it.")
    parser.add_argument(
        "--require-source-quiet-seconds",
        type=float,
        default=0.0,
        help="Wait for the source tree to stay unchanged for this many seconds before running.",
    )
    parser.add_argument(
        "--source-quiet-timeout-seconds",
        type=float,
        default=DEFAULT_SOURCE_QUIET_TIMEOUT_SECONDS,
        help="Maximum time to wait for the source quiet-window preflight.",
    )
    parser.add_argument(
        "--source-quiet-lease-seconds",
        type=float,
        default=DEFAULT_SOURCE_QUIET_LEASE_SECONDS,
        help=(
            "When source quiet preflight is required, hold a benchmark source lease "
            "for this many seconds so unrelated autonomous source edits can fail closed."
        ),
    )
    parser.add_argument(
        "--source-quiet-lease-path",
        type=Path,
        default=DEFAULT_SOURCE_QUIET_LEASE_PATH,
        help="Path to the benchmark source quiet lease file.",
    )
    parser.add_argument(
        "--source-write-preflight",
        action="store_true",
        help=(
            "Exit 0 when source/test edits are allowed, or 2 with a clear message "
            "when an active benchmark source quiet lease should block writes."
        ),
    )
    args = parser.parse_args(argv)

    if args.source_write_preflight:
        blocker = source_quiet_write_blocker(args.source_quiet_lease_path)
        if blocker:
            print(blocker, file=sys.stderr)
            return 2
        print("Source/test edits allowed: no active benchmark source quiet lease.")
        return 0

    all_scenarios = default_scenarios(include_mobile=True)
    if args.scenario:
        scenarios = select_scenarios(all_scenarios, args.scenario)
        profile = "custom"
    else:
        scenarios = default_scenarios(include_mobile=args.include_mobile)
        profile = "full" if args.include_mobile else "core"
    if args.list:
        for scenario in scenarios:
            print(
                f"{scenario.scenario_id}\t{scenario.category}\t"
                f"{','.join(scenario.capabilities)}\t{_command_text(scenario.command)}"
            )
        return 0

    source_quiet_lease: dict[str, object] | None = None
    try:
        if args.require_source_quiet_seconds > 0:
            try:
                source_quiet_lease = acquire_source_quiet_lease(
                    lease_path=args.source_quiet_lease_path,
                    holder="autopilot_coding_benchmark",
                    quiet_seconds=args.require_source_quiet_seconds,
                    lease_seconds=args.source_quiet_lease_seconds,
                    scenarios=scenarios,
                )
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            quiet = wait_for_source_quiet(
                REPO_ROOT,
                quiet_seconds=args.require_source_quiet_seconds,
                timeout_seconds=args.source_quiet_timeout_seconds,
            )
            if quiet.get("status") != "stable":
                preview = ", ".join(str(item) for item in quiet.get("changed_preview", []) or [])
                print(
                    "Source quiet preflight failed: "
                    f"status={quiet.get('status')}; "
                    f"required_quiet_seconds={quiet.get('quiet_seconds')}; "
                    f"observed_quiet_seconds={quiet.get('observed_quiet_seconds')}; "
                    f"changes={quiet.get('changed_count')}; "
                    f"preview={preview or 'none'}",
                    file=sys.stderr,
                )
                return 2

        results, markdown, output_path = run_benchmark(
            scenarios,
            output_path=args.output,
            write=not args.no_write,
            profile=profile,
        )
    finally:
        release_source_quiet_lease(
            source_quiet_lease,
            lease_path=args.source_quiet_lease_path,
        )
    if args.no_write:
        print(markdown)
    else:
        print(f"Wrote {output_path}")
    if args.allow_partial and args.scenario:
        return 0 if all(result.passed for result in results) else 1
    return 0 if benchmark_status(results) == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
