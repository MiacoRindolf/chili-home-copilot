"""Durable Project Brain Local Autopilot orchestration.

The orchestrator is intentionally local-first and safety-first:

* every run gets its own git worktree and integration branch;
* file and merge leases prevent concurrent autonomous edits from colliding;
* model calls prefer local Ollama models, with premium fallback left outside
  this module;
* merge only happens after validation and explicit gates pass.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import difflib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlparse

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ...models import (
    ProjectAutonomyArchitectReview,
    ProjectAutonomyArtifact,
    ProjectAutonomyLearningSample,
    ProjectAutonomyLease,
    ProjectAutonomyMessage,
    ProjectAutonomyRun,
    ProjectAutonomyStep,
    ProjectDomainRun,
)
from ...models.code_brain import CodeRepo
from ..code_brain import indexer as cb_indexer
from ..code_brain import insights as insights_mod
from ..code_brain.agent import (
    _MAX_FILES_PER_EDIT,
    _build_edit_prompt,
    _gather_context,
    _parse_plan_json,
    _read_file_content,
    _validate_diff,
)
from ..code_brain.runtime import resolve_repo_runtime_path
from ..code_dispatch import frozen_scope
from ..coding_task import workspaces as workspace_mod
from ..coding_task.envelope import subprocess_safe_env, truncate_text
from ..coding_task.validator_runner import (
    StepResult,
    run_ast_syntax,
    run_mypy_check,
    run_pytest_targeted,
    run_ruff_check,
)
from ..context_brain import ollama_client
from ..project_domain_runs import finish_run, start_run

AUTONOMOUS_KIND = "autonomous"
EXECUTION_MODE_PLAN_APPROVAL = "plan_approval"
EXECUTION_MODE_FULL_AUTOPILOT = "full_autopilot"
RUN_STATUS_AWAITING_APPROVAL = "awaiting_approval"
RUN_STATUS_AWAITING_CLARIFICATION = "awaiting_clarification"
RUN_STATUS_BLOCKED = "blocked"
RUN_STATUS_CANCELLED = "cancelled"
RUN_STATUS_CHATTING = "chatting"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_MERGED = "merged"
RUN_STATUS_MERGING = "merging"
RUN_STATUS_QUEUED = "queued"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_VALIDATING = "validating"
PLAN_STATUS_AWAITING_APPROVAL = "awaiting_approval"
PLAN_STATUS_AWAITING_CLARIFICATION = "awaiting_clarification"
PLAN_STATUS_APPROVED = "approved"
PLAN_STATUS_DRAFTING = "drafting"
PLAN_STATUS_IMPLEMENTED = "implemented"
PLAN_STATUS_REVISING = "revising"
MERGE_STATUS_PENDING = "pending"
STAGE_CHAT = "chat"
STAGE_CLASSIFY = "classify"
STAGE_IMPLEMENT = "implement"
STAGE_INTEGRATE = "integrate"
STAGE_LEARN = "learn"
STAGE_MERGE = "merge"
STAGE_PLAN = "plan"
STAGE_QUEUED = RUN_STATUS_QUEUED
STAGE_REPO_SCAN = "repo_scan"
STAGE_ASSIGN_ROLES = "assign_roles"
STAGE_VALIDATE = "validate"
STAGE_ARCHITECT_REVIEW = "architect_review"
ATTACHMENT_KIND_IMAGE = "image"
ATTACHMENT_ARTIFACT_TYPE_IMAGE = "prompt_image"
ATTACHMENT_IMAGE_MIME_PREFIX = "image/"
ATTACHMENT_DEFAULT_IMAGE_NAME = "attached image"
ATTACHMENT_CONTEXT_HEADING = "Attached images:"
ATTACHMENT_CONTEXT_LOCAL_SOURCE_LABEL = "local desktop image"
ATTACHMENT_CONTEXT_REMOTE_SOURCE_LABEL = "remote image URL"
ATTACHMENT_CONTEXT_SOURCELESS_LABEL = "image evidence"
ATTACHMENT_NAME_LIMIT = 180
ATTACHMENT_SOURCE_LIMIT = 900
ATTACHMENT_UNSAFE_PATH_REASON = "Image attachment path was rejected because it is not a safe absolute local file path."
ATTACHMENT_UNSAFE_URL_REASON = "Image attachment URL was rejected because only http and https URLs are allowed."
ATTACHMENT_PATH_TOO_LONG_REASON = "Image attachment path was rejected because it is too long."
ATTACHMENT_URL_TOO_LONG_REASON = "Image attachment URL was rejected because it is too long."
VISUAL_KIND_SCREENSHOT = "screenshot"
VISUAL_KIND_VIDEO = "video"
VISUAL_ARTIFACT_TYPE_SCREENSHOT = "visual_screenshot"
VISUAL_ARTIFACT_TYPE_VIDEO = "visual_video"
VISUAL_EVIDENCE_SOURCE_DESKTOP = "desktop"
VISUAL_EVIDENCE_SOURCE_URL = "url"
VISUAL_EVIDENCE_SOURCE_NONE = "none"
VISUAL_SCREENSHOT_UNAVAILABLE_REASON = "Screenshot evidence was requested, but no image capture was provided."
VISUAL_VIDEO_UNAVAILABLE_REASON = "Desktop video capture is not available yet for this run."
VISUAL_UNSAFE_PATH_REASON = "Visual evidence path was rejected because it is not a safe absolute local file path."
VISUAL_UNSAFE_URL_REASON = "Visual evidence URL was rejected because only http and https URLs are allowed."
VISUAL_PATH_TOO_LONG_REASON = "Visual evidence path was rejected because it is too long."
VISUAL_URL_TOO_LONG_REASON = "Visual evidence URL was rejected because it is too long."
VISUAL_SCREENSHOT_EXT_REASON = "Screenshot evidence path was rejected because it is not a supported image file."
VISUAL_VIDEO_EXT_REASON = "Video evidence path was rejected because it is not a supported video file."
OPERATOR_SAFE_PLAN_TEXT_LIMIT = 900
ERROR_SNIPPET_LIMIT = 900
CHAT_REPLY_LIMIT = 1800
WORKTREE_GIT_TIMEOUT_SEC = 180
PLAN_START_CHAT_ACTION_LABEL = "Start plan"
ARCHITECT_REVIEW_PASSING_SCORE = 85
ARCHITECT_REVIEW_MAX_ATTEMPTS = 3
ARCHITECT_REVIEW_STATUS_PASSED = "passed"
ARCHITECT_REVIEW_STATUS_FAILED = "failed"
ARCHITECT_REVIEW_STATUS_NEEDS_CLARIFICATION = "needs_clarification"
ARCHITECT_REVIEW_STATUS_NEEDS_REVISION = "needs_revision"
DESKTOP_AUTOPILOT_PRESENTER_FILE = "chili_mobile/lib/src/brain/autonomy_run_presenter.dart"
DESKTOP_AUTOPILOT_COCKPIT_FILE = "chili_mobile/lib/src/brain/brain_dispatch_screen.dart"
DESKTOP_NETWORK_ERROR_FILE = "chili_mobile/lib/src/network/network_error_message.dart"
DESKTOP_API_CLIENT_FILE = "chili_mobile/lib/src/network/chili_api_client.dart"
BROAD_DESKTOP_PLAN_ANALYSIS = (
    "Your request is broad, so I chose a small Autopilot cockpit polish instead of guessing across the app."
)
BROAD_DESKTOP_PLAN_DESCRIPTION = (
    "Improve Autopilot plan presentation so broad requests explain the chosen enhancement and approval path clearly."
)
BROAD_DESKTOP_PLAN_NOTES = (
    "Approval-first: send feedback to steer the enhancement, or approve to implement it in a worktree."
)
BROAD_DESKTOP_INTERNAL_REASON = "broad desktop enhancement routed to autopilot cockpit polish"
BROAD_DESKTOP_DETAIL_TOKENS = frozenset({
    "api",
    "auth",
    "backend url",
    "certificate",
    "connection",
    "error",
    "http",
    "json",
    "network",
    "response",
    "timeout",
})
DESKTOP_UI_PROMPT_TOKENS = frozenset({"app", "desktop", "flutter", "native", "screen", "ui"})
DESKTOP_AUTOPILOT_PROMPT_TOKENS = frozenset({"autonomous", "autopilot", "brain", "operator"})
DESKTOP_AUTOPILOT_ATTACHMENT_TOKENS = frozenset({
    "attach",
    "attached",
    "attachment",
    "file",
    "files",
    "image",
    "images",
    "photo",
    "photos",
    "picture",
    "pictures",
    "screenshot",
    "screenshots",
    "upload",
})
DESKTOP_AUTOPILOT_COMPOSER_TOKENS = frozenset({
    "chat",
    "composer",
    "enter",
    "input",
    "message",
    "prompt",
    "send",
    "textbox",
    "textarea",
})
DESKTOP_AUTOPILOT_PLAN_OVERRIDE_TOKENS = frozenset({
    "autopilot cockpit",
    "autopilot presenter",
    "autonomy_run_presenter",
    "broad desktop",
    "desktop-app enhancement",
    "plan presentation",
    "approval path",
})
DESKTOP_AUTOPILOT_PLAN_FILES = (
    DESKTOP_AUTOPILOT_PRESENTER_FILE,
    DESKTOP_AUTOPILOT_COCKPIT_FILE,
)
DESKTOP_AUTOPILOT_COCKPIT_INTENT_FILES = (
    DESKTOP_AUTOPILOT_COCKPIT_FILE,
    DESKTOP_AUTOPILOT_PRESENTER_FILE,
)
DESKTOP_SEEDED_FILES = (
    DESKTOP_AUTOPILOT_PRESENTER_FILE,
    DESKTOP_AUTOPILOT_COCKPIT_FILE,
    DESKTOP_NETWORK_ERROR_FILE,
    DESKTOP_API_CLIENT_FILE,
)
DESKTOP_FILE_PRIORITY = {
    DESKTOP_AUTOPILOT_PRESENTER_FILE: 0,
    DESKTOP_AUTOPILOT_COCKPIT_FILE: 1,
    DESKTOP_NETWORK_ERROR_FILE: 2,
    DESKTOP_API_CLIENT_FILE: 3,
}
DESKTOP_NETWORK_FILE_PRIORITY = {
    DESKTOP_API_CLIENT_FILE: 0,
    DESKTOP_NETWORK_ERROR_FILE: 1,
    DESKTOP_AUTOPILOT_PRESENTER_FILE: 5,
    DESKTOP_AUTOPILOT_COCKPIT_FILE: 6,
}
PRESENTER_PLAN_BODY_OLD_SNIPPET = """    final files = _mapList(plan['files'])
        .map((file) => _firstText(file, ['path', 'file']))
        .where((path) => path.isNotEmpty)
        .toList();
    final parts = <String>[];
    if (analysis.isNotEmpty) parts.add(analysis);
    if (files.isNotEmpty) {
      parts.add('Files: ${_listSummary(files, limit: 6)}.');
    }
"""
PRESENTER_PLAN_BODY_NEW_SNIPPET = """    final fileItems = _mapList(plan['files']);
    final files = fileItems
        .map((file) => _firstText(file, ['path', 'file']))
        .where((path) => path.isNotEmpty)
        .toList();
    final changes = fileItems
        .map((file) => _safePlanText(file['description']))
        .where((description) => description.isNotEmpty)
        .toList();
    final parts = <String>[];
    if (analysis.isNotEmpty) parts.add(analysis);
    if (changes.isNotEmpty) {
      parts.add('Plan: ${_listSummary(changes, limit: 3)}.');
    }
    if (files.isNotEmpty) {
      parts.add('Files: ${_listSummary(files, limit: 6)}.');
    }
"""
TERMINAL_STATUSES = frozenset({
    RUN_STATUS_MERGED,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_BLOCKED,
    RUN_STATUS_FAILED,
    RUN_STATUS_CANCELLED,
})
IDLE_STATUSES = frozenset({
    RUN_STATUS_AWAITING_APPROVAL,
    RUN_STATUS_AWAITING_CLARIFICATION,
    RUN_STATUS_CHATTING,
})
ACTIVE_STATUSES = frozenset({
    RUN_STATUS_QUEUED,
    RUN_STATUS_RUNNING,
    RUN_STATUS_VALIDATING,
    RUN_STATUS_MERGING,
    PLAN_STATUS_REVISING,
})
AGENT_OS_READINESS_CHECK_PASSED = "passed"
AGENT_OS_READINESS_CHECK_WARNING = "warning"
AGENT_CODING_BENCHMARK_SCORECARD_REL_PATH = "project_ws/AgentOps/CODING_BENCHMARK_SCORECARD.md"
AGENT_CODING_BENCHMARK_REPAIRED_ROWS_REL_PATH = (
    "project_ws/AgentOps/CODING_BENCHMARK_REPAIRED_AUTOPILOT_ROWS.md"
)
AGENT_SOURCE_CHURN_DIAGNOSTICS_REL_PATH = "project_ws/AgentOps/SOURCE_CHURN_DIAGNOSTICS.md"
AGENT_SYNTHETIC_REPO_REPAIR_SCORECARD_REL_PATH = "project_ws/AgentOps/SYNTHETIC_REPO_REPAIR_BENCHMARK.md"
AGENT_MODEL_PROMOTION_SCORECARD_REL_PATH = "project_ws/AgentOps/MODEL_PROMOTION_REPLAY_BENCHMARK.md"
AGENT_MODEL_SHADOW_EVIDENCE_SCORECARD_REL_PATH = "project_ws/AgentOps/MODEL_SHADOW_EVIDENCE_BENCHMARK.md"
AGENT_MODEL_CANDIDATE_TOURNAMENT_SCORECARD_REL_PATH = "project_ws/AgentOps/MODEL_CANDIDATE_TOURNAMENT_BENCHMARK.md"
AGENT_HOSTED_PR_REPAIR_SCORECARD_REL_PATH = "project_ws/AgentOps/HOSTED_PR_REPAIR_ARTIFACT_BENCHMARK.md"
AGENT_HOSTED_PR_REPAIR_REPORT_GLOB = "PR_*_CI_REPAIR.md"
AGENT_FRONTIER_EVIDENCE_PREFLIGHT_REL_PATH = "project_ws/AgentOps/FRONTIER_EVIDENCE_PREFLIGHT.md"
AGENT_FRONTIER_EVIDENCE_PREFLIGHT_LIVE_REL_PATH = (
    "project_ws/AgentOps/FRONTIER_EVIDENCE_PREFLIGHT_LIVE.md"
)
AGENT_FRONTIER_PROMPT_PACK_MANIFEST_REL_PATH = (
    "project_ws/AgentOps/frontier_model_prompt_packs/manifest.json"
)
AGENT_FRONTIER_MODEL_EVIDENCE_RAW_SOURCES_REL_PATH = (
    "project_ws/AgentOps/frontier_model_evidence_intake/raw_sources"
)
AGENT_FRONTIER_MODEL_EVIDENCE_COLLECTION_PACKETS_REL_PATH = (
    "project_ws/AgentOps/frontier_model_evidence_intake/collection_packets"
)
AGENT_FRONTIER_MODEL_EVIDENCE_OUTPUT_ROOT_REL_PATH = (
    "project_ws/AgentOps/frontier_model_evidence_intake"
)
AGENT_LOCAL_MODEL_CANDIDATE_RUN_REL_PATH = "project_ws/AgentOps/LOCAL_MODEL_CANDIDATE_RUN.md"
AGENT_FRONTIER_MODEL_EVIDENCE_SETUP_COMMAND = (
    "python scripts/autopilot_frontier_model_evidence_setup.py --json"
)
AGENT_FRONTIER_SOURCE_COLLECTION_PACKET_COMMAND = (
    "python scripts/autopilot_frontier_source_collection_packet.py --json"
)
AGENT_FRONTIER_SOURCE_EVIDENCE_RECORD_COMMAND = (
    "python scripts/autopilot_frontier_source_evidence_recorder.py "
    "--source-kind <codex|claude|local_model> --case-id <case-id> "
    "--response <model-response.txt> --run-id <real-source-run-id> "
    "--source-command <exact-model-command-or-session-export> --json"
)
AGENT_FRONTIER_SOURCE_EVIDENCE_RECORD_ALL_CASES_COMMAND = (
    "python scripts/autopilot_frontier_source_evidence_recorder.py "
    "--source-kind <codex|claude|local_model> --all-cases "
    "--response <model-response.txt> --run-id <real-source-run-id> "
    "--source-command <exact-model-command-or-session-export> --json"
)
AGENT_LOCAL_MODEL_EVIDENCE_RECORD_COMMAND = (
    "python scripts/autopilot_local_model_evidence_recorder.py "
    "--drop-dir <local-model-drop-dir> --response <local-model-response.txt> "
    "--run-id <real-local-run-id> --source-command <exact-local-model-command> --json"
)
AGENT_LOCAL_MODEL_CANDIDATE_RUN_COMMAND = (
    "python scripts/autopilot_local_model_candidate_runner.py "
    "--all-cases --json"
)
AGENT_SOURCE_CHURN_DIAGNOSTICS_COMMAND = (
    "python scripts/autopilot_source_churn_diagnostics.py "
    "--watch-seconds 30 --json"
)
AGENT_FRONTIER_RESPONSE_IMPORT_CASE_ID = "real-chili-preflight-candidate-wins"
AGENT_HOSTED_PR_REPAIR_ARTIFACT_VALIDATE_COMMAND = (
    "python scripts/autopilot_hosted_pr_repair_artifact_benchmark.py "
    "--artifact-dir <hosted-pr-repair-artifact-dir> --json"
)
AGENT_HOSTED_PR_REPAIR_COLLECTION_PACKET_COMMAND = (
    "python scripts/autopilot_hosted_pr_repair_collection_packet.py --json"
)
AGENT_HOSTED_PR_REPAIR_EVIDENCE_COLLECTOR_COMMAND = (
    "python scripts/autopilot_hosted_pr_repair_evidence_collector.py --json"
)
AGENT_HOSTED_PR_REPAIR_ARTIFACT_ASSEMBLER_COMMAND = (
    "python scripts/autopilot_hosted_pr_repair_artifact_assembler.py --json"
)
AGENT_MODEL_SHADOW_EVIDENCE_SCHEMA_VERSION = "chili.model-shadow-evidence-benchmark.v1"
AGENT_MODEL_CANDIDATE_TOURNAMENT_SCHEMA_VERSION = "chili.model-candidate-tournament-benchmark.v1"
AGENT_HOSTED_PR_REPAIR_SCHEMA_VERSION = "chili.hosted-pr-repair-artifact-benchmark.v1"
AGENT_CODING_BENCHMARK_TARGET_SCORE = 90
AGENT_CODING_BENCHMARK_MIN_SCENARIOS = 6
AGENT_MODEL_SHADOW_EVIDENCE_MIN_CHECKS = 7
AGENT_MODEL_CANDIDATE_TOURNAMENT_MIN_CASES = 6
AGENT_HOSTED_PR_REPAIR_MIN_CHECKS = 18
AGENT_MODEL_SHADOW_REQUIRED_EVIDENCE_MODE = "real_manifest"
AGENT_MODEL_CANDIDATE_TOURNAMENT_REQUIRED_EVIDENCE_MODE = "real_artifacts"
AGENT_HOSTED_PR_REPAIR_REQUIRED_EVIDENCE_MODE = "real_inventory"
try:
    from scripts.autopilot_coding_benchmark import REQUIRED_CAPABILITIES as _CODING_REQUIRED_CAPABILITIES
except Exception:
    _CODING_REQUIRED_CAPABILITIES = ()
AGENT_CODING_BENCHMARK_REQUIRED_CAPABILITIES = tuple(_CODING_REQUIRED_CAPABILITIES)
AGENT_FRONTIER_MODEL_EVIDENCE_SOURCE_KINDS = ("codex", "claude", "local_model")
AGENT_FRONTIER_MODEL_EVIDENCE_REQUIRED_SOURCE_FILES = (
    "metadata.json",
    "prompt_pack.md",
    "transcript.jsonl",
)
AGENT_HOSTED_PR_REPAIR_REQUIRED_CHECKS = (
    "valid_hosted_pr_repair_accepts",
    "self_test_artifact_rejected",
    "missing_review_thread_transcript_rejected",
    "sparse_review_transcript_rejected",
    "review_transcript_pr_mismatch_rejected",
    "review_transcript_thread_detail_mismatch_rejected",
    "missing_line_thread_rejected",
    "missing_remote_publication_rejected",
    "post_repair_head_mismatch_rejected",
    "missing_post_repair_check_receipt_rejected",
    "transcript_hash_mismatch_rejected",
    "sparse_publication_transcript_rejected",
    "publication_transcript_pr_mismatch_rejected",
    "publication_transcript_commit_mismatch_rejected",
    "valid_artifact_inventory_accepts",
    "empty_artifact_inventory_rejected",
    "duplicate_pr_artifact_rejected",
    "duplicate_source_run_rejected",
)
AGENT_CODING_BENCHMARK_SOURCE_ROOTS = (
    "app",
    "tests",
    "scripts",
    "chili_mobile/lib",
    "chili_mobile/test",
)
AGENT_CODING_BENCHMARK_SOURCE_SUFFIXES = frozenset(
    {
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
    }
)
AGENT_CODING_BENCHMARK_SOURCE_SKIP_DIRS = frozenset(
    {
        ".dart_tool",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        "__pycache__",
        "build",
        "node_modules",
        "project_ws",
    }
)
AGENT_CODING_BENCHMARK_FRESHNESS_PREVIEW_LIMIT = 8
_CODING_BENCHMARK_FAILED_ROW_STATUSES = frozenset(
    {"failed", "timed_out", "error", "blocked", "environment_blocked"}
)
_CODING_BENCHMARK_REPAIRED_ROW_STATUSES = frozenset(
    {"passed", "pass", "repaired", "accepted", "ok", "success", "successful"}
)


def _scorecard_metadata(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    metadata: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line.startswith("- ") or ":" not in line:
            continue
        key, value = line[2:].split(":", 1)
        metadata[key.strip().lower()] = value.strip()
    return metadata


def _scorecard_table_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    headers: list[str] = []
    rows: list[dict[str, str]] = []
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if not cells:
            continue
        if all(set(cell.replace(" ", "")) <= {"-", ":"} for cell in cells):
            continue
        if not headers:
            headers = [cell.lower() for cell in cells]
            continue
        if len(cells) < len(headers):
            continue
        rows.append(dict(zip(headers, cells, strict=False)))
    return rows


def _markdown_table_rows_after_heading(path: Path, heading: str) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    target = f"## {heading}".strip().lower()
    headers: list[str] = []
    rows: list[dict[str, str]] = []
    in_section = False
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            if in_section and line.lower() != target:
                break
            in_section = line.lower() == target
            headers = []
            continue
        if not in_section or not line.startswith("|") or not line.endswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if not cells:
            continue
        if all(set(cell.replace(" ", "")) <= {"-", ":"} for cell in cells):
            continue
        if not headers:
            headers = [cell.lower() for cell in cells]
            continue
        if len(cells) < len(headers):
            continue
        rows.append(dict(zip(headers, cells, strict=False)))
    return rows


def _scorecard_int(metadata: Mapping[str, str], key: str, default: int = 0) -> int:
    value = str(metadata.get(key.lower()) or "").strip()
    if "/" in value:
        value = value.split("/", 1)[0].strip()
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _scorecard_text(metadata: Mapping[str, str], key: str, default: str = "") -> str:
    return str(metadata.get(key.lower()) or default).strip()


def _scorecard_missing_capabilities(metadata: Mapping[str, str]) -> list[str]:
    coverage = _scorecard_text(metadata, "capability coverage").lower()
    if not coverage or coverage == "none":
        return list(AGENT_CODING_BENCHMARK_REQUIRED_CAPABILITIES)
    return [
        capability
        for capability in AGENT_CODING_BENCHMARK_REQUIRED_CAPABILITIES
        if capability.lower() not in coverage
    ]


def _scorecard_generated_utc(metadata: Mapping[str, str]) -> datetime | None:
    raw = _scorecard_text(metadata, "generated utc")
    if not raw:
        return None
    value = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iter_coding_benchmark_source_files(runtime_path: Path) -> Iterable[Path]:
    for rel_root in AGENT_CODING_BENCHMARK_SOURCE_ROOTS:
        root = runtime_path / Path(rel_root)
        if not root.exists():
            continue
        candidates = [root] if root.is_file() else root.rglob("*")
        for path in sorted(candidates):
            if not path.is_file():
                continue
            try:
                rel_parts = path.relative_to(runtime_path).parts
            except ValueError:
                rel_parts = path.parts
            if any(part in AGENT_CODING_BENCHMARK_SOURCE_SKIP_DIRS for part in rel_parts):
                continue
            if path.suffix.lower() in AGENT_CODING_BENCHMARK_SOURCE_SUFFIXES:
                yield path


def _scorecard_source_freshness(runtime_path: Path, metadata: Mapping[str, str]) -> dict[str, Any]:
    generated_raw = _scorecard_text(metadata, "generated utc")
    generated_at = _scorecard_generated_utc(metadata)
    if not generated_raw:
        return {
            "status": "missing_generated_utc",
            "generated_utc": "",
            "source_changes_after_scorecard": 0,
            "source_change_preview_after_scorecard": "none",
            "changed_files": [],
        }
    if generated_at is None:
        return {
            "status": "invalid_generated_utc",
            "generated_utc": generated_raw,
            "source_changes_after_scorecard": 0,
            "source_change_preview_after_scorecard": "none",
            "changed_files": [],
        }

    changed_files: list[str] = []
    changed_count = 0
    for path in _iter_coding_benchmark_source_files(runtime_path):
        try:
            changed_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if changed_at <= generated_at:
            continue
        changed_count += 1
        if len(changed_files) < AGENT_CODING_BENCHMARK_FRESHNESS_PREVIEW_LIMIT:
            try:
                changed_files.append(path.relative_to(runtime_path).as_posix())
            except ValueError:
                changed_files.append(path.as_posix())

    return {
        "status": "current" if changed_count == 0 else "stale",
        "generated_utc": generated_raw,
        "source_changes_after_scorecard": changed_count,
        "source_change_preview_after_scorecard": (
            "none" if not changed_files else ", ".join(changed_files)
        ),
        "changed_files": changed_files,
    }


def _source_churn_diagnostics_summary(runtime_path: Path) -> dict[str, Any]:
    rel_path = AGENT_SOURCE_CHURN_DIAGNOSTICS_REL_PATH
    report_path = runtime_path / Path(rel_path)
    if not report_path.is_file():
        return {
            "present": False,
            "status": "missing",
            "path": rel_path,
            "generated_utc": "",
            "promotion_impact": "unknown",
            "rerun_readiness": "unknown",
            "scorecard_status": "",
            "scorecard_source_stability": "",
            "source_changes_during_scorecard": 0,
            "current_source_freshness": "",
            "source_changes_after_scorecard": 0,
            "watch_status": "",
            "watch_seconds": "",
            "source_changes_during_watch": 0,
            "changed_files": [],
            "changed_file_preview": "none",
            "watch_changed_files": [],
            "watch_change_preview": "none",
            "next_action": (
                f"Run {AGENT_SOURCE_CHURN_DIAGNOSTICS_COMMAND}; then wait for "
                "source/test churn to settle and rerun the full coding benchmark "
                "with a source quiet preflight."
            ),
        }

    metadata = _scorecard_metadata(report_path)
    newer_rows = _markdown_table_rows_after_heading(report_path, "Files Newer Than Scorecard")
    watch_rows = _markdown_table_rows_after_heading(report_path, "Files Changed During Watch")

    def _paths(rows: Sequence[Mapping[str, str]]) -> list[str]:
        paths: list[str] = []
        for row in rows:
            path = str(row.get("path") or "").strip()
            if path and path.lower() != "none":
                paths.append(path)
        return paths

    changed_files = _paths(newer_rows)
    watch_changed_files = _paths(watch_rows)
    changed_file_preview = (
        "none"
        if not changed_files
        else ", ".join(changed_files[:AGENT_CODING_BENCHMARK_FRESHNESS_PREVIEW_LIMIT])
    )
    watch_change_preview = (
        "none"
        if not watch_changed_files
        else ", ".join(watch_changed_files[:AGENT_CODING_BENCHMARK_FRESHNESS_PREVIEW_LIMIT])
    )
    return {
        "present": True,
        "status": _scorecard_text(metadata, "status", "unknown").lower(),
        "path": rel_path,
        "generated_utc": _scorecard_text(metadata, "generated utc"),
        "promotion_impact": _scorecard_text(metadata, "promotion impact", "unknown"),
        "rerun_readiness": _scorecard_text(metadata, "rerun readiness", "unknown"),
        "scorecard_status": _scorecard_text(metadata, "scorecard status"),
        "scorecard_source_stability": _scorecard_text(
            metadata,
            "scorecard source stability",
        ),
        "source_changes_during_scorecard": _scorecard_int(
            metadata,
            "source changes during scorecard",
        ),
        "current_source_freshness": _scorecard_text(
            metadata,
            "current source freshness",
        ),
        "source_changes_after_scorecard": _scorecard_int(
            metadata,
            "source changes after scorecard",
        ),
        "watch_status": _scorecard_text(metadata, "watch status"),
        "watch_seconds": _scorecard_text(metadata, "watch seconds"),
        "source_changes_during_watch": _scorecard_int(
            metadata,
            "source changes during watch",
        ),
        "changed_files": changed_files,
        "changed_file_preview": changed_file_preview,
        "watch_changed_files": watch_changed_files,
        "watch_change_preview": watch_change_preview,
        "next_action": _scorecard_text(
            metadata,
            "next action",
            "Rerun the source churn diagnostic, then rerun the full coding benchmark.",
        ),
    }


def _scorecard_row_status(row: Mapping[str, str]) -> str:
    return str(row.get("result") or row.get("status") or "").strip().lower()


def _scorecard_row_id(row: Mapping[str, str]) -> str:
    return str(
        row.get("scenario id")
        or row.get("scenario")
        or row.get("case")
        or row.get("case id")
        or ""
    ).strip()


def _scorecard_row_capabilities(row: Mapping[str, str]) -> set[str]:
    raw = str(row.get("capability") or row.get("capabilities") or "").strip()
    if not raw:
        return set()
    return {
        item.strip()
        for item in raw.split(",")
        if item.strip()
    }


def _coding_benchmark_repaired_failed_rows(
    runtime_path: Path,
    primary_metadata: Mapping[str, str],
    primary_missing_capabilities: Sequence[str] = (),
) -> dict[str, Any]:
    primary_rows = _scorecard_table_rows(
        runtime_path / Path(AGENT_CODING_BENCHMARK_SCORECARD_REL_PATH)
    )
    failed_ids = {
        row_id
        for row in primary_rows
        for row_id in [_scorecard_row_id(row)]
        if row_id and _scorecard_row_status(row) in _CODING_BENCHMARK_FAILED_ROW_STATUSES
    }
    repair_path = runtime_path / Path(AGENT_CODING_BENCHMARK_REPAIRED_ROWS_REL_PATH)
    repair_metadata = _scorecard_metadata(repair_path)
    repair_rows = _scorecard_table_rows(repair_path)
    repaired_ids = {
        row_id
        for row in repair_rows
        for row_id in [_scorecard_row_id(row)]
        if row_id and _scorecard_row_status(row) in _CODING_BENCHMARK_REPAIRED_ROW_STATUSES
    }
    repaired_capabilities = sorted(
        {
            capability
            for row in repair_rows
            if _scorecard_row_status(row) in _CODING_BENCHMARK_REPAIRED_ROW_STATUSES
            for capability in _scorecard_row_capabilities(row)
        }
    )
    primary_generated = _scorecard_generated_utc(primary_metadata)
    repair_generated = _scorecard_generated_utc(repair_metadata)
    repair_is_newer = bool(
        primary_generated is not None
        and repair_generated is not None
        and repair_generated > primary_generated
    )
    covered_ids = sorted(failed_ids & repaired_ids) if repair_is_newer else []
    missing_ids = sorted(failed_ids - repaired_ids) if repair_is_newer else sorted(failed_ids)
    primary_missing_set = set(primary_missing_capabilities)
    covered_capabilities = (
        sorted(primary_missing_set & set(repaired_capabilities))
        if repair_is_newer
        else []
    )
    missing_capabilities_after_repair = (
        sorted(primary_missing_set - set(covered_capabilities))
        if repair_is_newer
        else sorted(primary_missing_set)
    )
    return {
        "path": AGENT_CODING_BENCHMARK_REPAIRED_ROWS_REL_PATH,
        "repair_is_newer": repair_is_newer,
        "failed_ids": sorted(failed_ids),
        "covered_ids": covered_ids,
        "missing_ids": missing_ids,
        "covers_all_failed_rows": bool(failed_ids and repair_is_newer and not missing_ids),
        "repaired_capabilities": repaired_capabilities if repair_is_newer else [],
        "covered_missing_capabilities": covered_capabilities,
        "missing_capabilities_after_repair": missing_capabilities_after_repair,
        "covers_all_missing_capabilities": bool(
            primary_missing_set
            and repair_is_newer
            and not missing_capabilities_after_repair
        ),
    }


def _dependent_scorecard_problem(
    runtime_path: Path,
    rel_path: str,
    *,
    min_checks: int | None = None,
    min_cases: int | None = None,
    required_evidence_mode: str | None = None,
    required_metadata: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    path = runtime_path / Path(rel_path)
    metadata = _scorecard_metadata(path)
    problems: list[str] = []
    if not metadata:
        return {"status": AGENT_OS_READINESS_CHECK_WARNING, "path": rel_path}, [f"missing {rel_path}"]
    status = _scorecard_text(metadata, "status").lower()
    if status != AGENT_OS_READINESS_CHECK_PASSED:
        problems.append(f"{rel_path} status is {status or 'missing'}")
    checks = _scorecard_int(metadata, "checks")
    if min_checks is not None and checks < min_checks:
        problems.append(f"{rel_path} check count is below {min_checks}")
    cases = _scorecard_int(metadata, "cases")
    if min_cases is not None and cases < min_cases:
        problems.append(f"{rel_path} case count is below {min_cases}")
    evidence_mode = _scorecard_text(metadata, "evidence mode")
    if required_evidence_mode and evidence_mode != required_evidence_mode:
        problems.append(
            f"{rel_path} evidence mode is {evidence_mode or 'missing'} instead of {required_evidence_mode}"
        )
    missing_checks = _scorecard_text(metadata, "missing checks")
    if missing_checks and missing_checks.lower() != "none":
        problems.append(f"{rel_path} missing checks: {missing_checks}")
    metadata_values: dict[str, str] = {}
    for key, expected in (required_metadata or {}).items():
        actual = _scorecard_text(metadata, key)
        metadata_values[key] = actual
        if actual != expected:
            problems.append(f"{rel_path} {key} is {actual or 'missing'} instead of {expected}")
    return (
        {
            "status": (
                AGENT_OS_READINESS_CHECK_PASSED
                if not problems
                else AGENT_OS_READINESS_CHECK_WARNING
            ),
            "path": rel_path,
            "check_count": checks,
            "case_count": cases,
            "evidence_mode": evidence_mode,
            "metadata_values": metadata_values,
            "contract_problems": problems,
        },
        problems,
    )


def _frontier_evidence_gap(
    *,
    gate: str,
    label: str,
    required: str,
    actual: str,
    path: str,
    next_action: str,
    problems: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "gate": gate,
        "label": label,
        "required": required,
        "actual": actual or "missing",
        "path": path,
        "next_action": next_action,
        "problems": list(problems),
    }


def _synthesized_local_model_candidate_recovery_routes(
    *,
    status: str,
    model_name: str,
    failed_case_id: str,
    failure_stage: str,
    failure_reason: str,
    diagnostics_path: str = "",
    prompt_path: str = "",
    response_path: str = "",
) -> list[dict[str, Any]]:
    if status != "failed" or not failed_case_id:
        return []
    safe_case = re.sub(r"[^a-z0-9._-]+", "-", failed_case_id.lower()).strip(".-") or "case"
    timeout_seconds = 300
    match = re.search(r"timed out after\s+(\d+)s", failure_reason.lower())
    if match:
        timeout_seconds = max(300, int(match.group(1)) * 2)
    if failure_stage == "model" and "timed out" in failure_reason.lower():
        action_label = "Retry failed case with longer timeout"
        reason = "The local model timed out before producing a parseable candidate."
    elif failure_stage == "parse":
        action_label = "Import corrected failed-case response"
        reason = "The local model produced output, but CHILI could not parse a valid candidate JSON/diff."
    else:
        action_label = "Retry failed local-model case"
        reason = "The local-model suite stopped before all cases produced verified candidates."
    model = model_name or "qwen3:4b"
    retry_command = (
        "python scripts/autopilot_local_model_candidate_runner.py "
        f"--retry-from-diagnostics {diagnostics_path} "
        f"--timeout-seconds {timeout_seconds} --json"
        if diagnostics_path
        else (
            "python scripts/autopilot_local_model_candidate_runner.py "
            f"--case-id {failed_case_id} --model-name {model} "
            f"--timeout-seconds {timeout_seconds} --json"
        )
    )
    import_response_command = (
        "python scripts/autopilot_local_model_candidate_runner.py "
        f"--retry-from-diagnostics {diagnostics_path} "
        f"--response-file <local-model-{safe_case}-response.txt> "
        "--run-id <real-local-run-id> "
        "--source-command <exact-local-model-command> --json"
        if diagnostics_path
        else (
            "python scripts/autopilot_local_model_candidate_runner.py "
            f"--case-id {failed_case_id} --model-name {model} "
            f"--response-file <local-model-{safe_case}-response.txt> "
            "--run-id <real-local-run-id> "
            "--source-command <exact-local-model-command> --json"
        )
    )
    return [
        {
            "status": "available",
            "case_id": failed_case_id,
            "action_label": action_label,
            "reason": reason,
            "retry_command": retry_command,
            "import_response_command": import_response_command,
            "prompt_path": prompt_path,
            "response_path": response_path,
            "permission_boundary": (
                "local model diagnostics and evidence import only; no source/test edits, "
                "git/PR action, runtime restart, deployment, database migration, broker call, "
                "or live trading"
            ),
        }
    ]


def _local_model_candidate_run_status(runtime_path: Path) -> dict[str, Any]:
    rel_path = AGENT_LOCAL_MODEL_CANDIDATE_RUN_REL_PATH
    report_path = runtime_path / Path(rel_path)
    metadata = _scorecard_metadata(report_path)
    if not metadata:
        return {
            "status": "missing",
            "path": rel_path,
            "failed": False,
            "case_id": "",
            "timeout_salvaged_cases": [],
            "timeout_salvaged_case_count": 0,
            "failed_case_id": "",
            "failure_stage": "",
            "failure_reason": "",
            "diagnostics": "",
            "recovery_routes": [],
            "recovery_route_count": 0,
            "next_action": "",
        }

    diagnostics_text = _scorecard_text(metadata, "diagnostics")
    artifact_paths: dict[str, str] = {}
    for row in _scorecard_table_rows(report_path):
        artifact = str(row.get("artifact") or "").strip().lower()
        path = str(row.get("path") or "").strip()
        if artifact and path:
            artifact_paths[artifact] = path
    if not diagnostics_text:
        diagnostics_text = artifact_paths.get("diagnostics", "")
    diagnostics_path = Path(diagnostics_text) if diagnostics_text else Path()
    if diagnostics_text and not diagnostics_path.is_absolute():
        diagnostics_path = runtime_path / diagnostics_path
    diagnostics: Mapping[str, Any] = {}
    if diagnostics_text and diagnostics_path.is_file():
        try:
            loaded = json.loads(diagnostics_path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            loaded = {}
        if isinstance(loaded, Mapping):
            diagnostics = loaded
    status = _scorecard_text(metadata, "status", "missing")
    model_name = _scorecard_text(metadata, "model")
    failure_reason = _scorecard_text(metadata, "failure reason") or str(
        diagnostics.get("failure_reason") or ""
    ).strip()
    failed_case_id = _scorecard_text(metadata, "failed case") or str(
        diagnostics.get("failed_case_id") or ""
    ).strip()
    failure_stage = _scorecard_text(metadata, "failure stage") or str(
        diagnostics.get("failure_stage") or ""
    ).strip()
    timeout_salvaged_cases_text = _scorecard_text(metadata, "timeout salvaged cases")
    timeout_salvaged_cases = [
        item.strip()
        for item in re.split(r"[,;\n]+", timeout_salvaged_cases_text)
        if item.strip()
    ]
    if not timeout_salvaged_cases:
        diagnostic_salvaged = diagnostics.get("timeout_salvaged_cases")
        if isinstance(diagnostic_salvaged, (list, tuple)):
            timeout_salvaged_cases = [
                str(item).strip()
                for item in diagnostic_salvaged
                if str(item).strip()
            ]
    if not timeout_salvaged_cases:
        timeout_salvaged_cases = [
            str(result.get("case_id") or "").strip()
            for result in diagnostics.get("case_results") or []
            if isinstance(result, Mapping)
            and result.get("timeout_salvaged") is True
            and str(result.get("case_id") or "").strip()
        ]
    failed_case_result: Mapping[str, Any] = {}
    for result in diagnostics.get("case_results") or []:
        if not isinstance(result, Mapping):
            continue
        if str(result.get("case_id") or "").strip() == failed_case_id:
            failed_case_result = result
            break
    failed_prompt_path = str(failed_case_result.get("prompt") or "").strip()
    failed_response_path = str(failed_case_result.get("response") or "").strip()
    recovery_routes = [
        dict(route)
        for route in diagnostics.get("recovery_routes") or []
        if isinstance(route, Mapping)
    ]
    for route in recovery_routes:
        route.setdefault("prompt_path", failed_prompt_path)
        route.setdefault("response_path", failed_response_path)
    if not recovery_routes:
        recovery_routes = _synthesized_local_model_candidate_recovery_routes(
            status=status,
            model_name=model_name,
            failed_case_id=failed_case_id,
            failure_stage=failure_stage,
            failure_reason=failure_reason,
            diagnostics_path=diagnostics_text,
            prompt_path=failed_prompt_path,
            response_path=failed_response_path,
        )
    next_action = _scorecard_text(metadata, "next action")
    if recovery_routes:
        first_route = recovery_routes[0]
        retry = str(first_route.get("retry_command") or "").strip()
        imported = str(first_route.get("import_response_command") or "").strip()
        if retry and imported:
            next_action = f"{retry}; or import a saved response with: {imported}"
        elif retry:
            next_action = retry
        elif imported:
            next_action = imported
    return {
        "status": status,
        "path": rel_path,
        "failed": status == "failed",
        "case_id": _scorecard_text(metadata, "case"),
        "cases": _scorecard_int(metadata, "cases"),
        "model_name": model_name,
        "run_id": _scorecard_text(metadata, "run id"),
        "timeout_salvaged_cases": timeout_salvaged_cases,
        "timeout_salvaged_case_count": len(timeout_salvaged_cases),
        "failed_case_id": failed_case_id,
        "failure_stage": failure_stage,
        "failure_reason": failure_reason,
        "diagnostics": diagnostics_text,
        "artifacts": artifact_paths,
        "prompt_pack": artifact_paths.get("prompt_pack", ""),
        "full_prompt_pack": artifact_paths.get("full_prompt_pack", ""),
        "response": artifact_paths.get("response", ""),
        "failed_prompt": failed_prompt_path,
        "failed_response": failed_response_path,
        "recovery_routes": recovery_routes,
        "recovery_route_count": len(recovery_routes),
        "next_action": next_action,
    }


def _frontier_evidence_gap_summary(
    *,
    source_stability: str,
    source_changes: int,
    source_freshness: Mapping[str, Any],
    source_churn_diagnostics: Mapping[str, Any],
    model_shadow: Mapping[str, Any],
    model_tournament: Mapping[str, Any],
    hosted_pr_repair: Mapping[str, Any],
    local_model_candidate_run: Mapping[str, Any],
    frontier_model_evidence_intake: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    model_intake = frontier_model_evidence_intake or {}
    model_intake_next_action = str(model_intake.get("next_action") or "").strip()
    model_intake_status = str(model_intake.get("status") or "").strip()
    model_evidence_publish_command = (
        "python scripts/autopilot_frontier_model_evidence_intake.py "
        f"--input-root {AGENT_FRONTIER_MODEL_EVIDENCE_RAW_SOURCES_REL_PATH} "
        f"--output-root {AGENT_FRONTIER_MODEL_EVIDENCE_OUTPUT_ROOT_REL_PATH} "
        "--publish-scorecards --json"
    )
    if model_intake_status == "ready":
        model_evidence_next_action = (
            f"Publish real model scorecards with: {model_evidence_publish_command}."
        )
    elif model_intake_next_action:
        model_evidence_next_action = (
            f"Close source intake first: {model_intake_next_action} "
            f"Then publish real model scorecards with: {model_evidence_publish_command}."
        )
    else:
        model_evidence_next_action = (
            "Collect transcript-verified Codex, Claude, and local-model drops; "
            f"then publish real model scorecards with: {model_evidence_publish_command}."
        )
    freshness_status = str(source_freshness.get("status") or "")
    if source_stability != "stable" or source_changes or freshness_status == "stale":
        after_count = int(source_freshness.get("source_changes_after_scorecard") or 0)
        actual_parts = [
            f"source stability {source_stability or 'missing'}",
            f"changes during run {source_changes}",
        ]
        if after_count:
            actual_parts.append(f"changes after scorecard {after_count}")
        diagnostic_present = bool(source_churn_diagnostics.get("present"))
        diagnostic_path = str(
            source_churn_diagnostics.get("path")
            or AGENT_SOURCE_CHURN_DIAGNOSTICS_REL_PATH
        )
        if diagnostic_present:
            diagnostic_status = str(source_churn_diagnostics.get("status") or "").strip()
            rerun_readiness = str(
                source_churn_diagnostics.get("rerun_readiness") or ""
            ).strip()
            watch_status = str(source_churn_diagnostics.get("watch_status") or "").strip()
            if diagnostic_status:
                actual_parts.append(f"diagnostic {diagnostic_status}")
            if rerun_readiness:
                actual_parts.append(f"rerun {rerun_readiness}")
            if watch_status:
                actual_parts.append(f"watch {watch_status}")
            diagnostic_next_action = str(
                source_churn_diagnostics.get("next_action") or ""
            ).strip()
            next_action = (
                f"Latest diagnostic at {diagnostic_path}: "
                f"{diagnostic_next_action or 'rerun the full coding benchmark after source/test churn settles.'} "
                f"Refresh with {AGENT_SOURCE_CHURN_DIAGNOSTICS_COMMAND} if edits resume."
            )
            diagnostic_problem = str(
                source_churn_diagnostics.get("changed_file_preview") or ""
            ).strip()
            problems = (
                [f"diagnostic changed files: {diagnostic_problem}"]
                if diagnostic_problem and diagnostic_problem != "none"
                else []
            )
        else:
            next_action = (
                f"Run {AGENT_SOURCE_CHURN_DIAGNOSTICS_COMMAND}; then wait for "
                "source/test churn to settle and rerun the full coding benchmark "
                "with a source quiet preflight."
            )
            problems = []
        gaps.append(
            _frontier_evidence_gap(
                gate="source_freshness",
                label="source freshness",
                required="stable benchmark with no newer source/test files",
                actual="; ".join(actual_parts),
                path=diagnostic_path if diagnostic_present else AGENT_CODING_BENCHMARK_SCORECARD_REL_PATH,
                next_action=next_action,
                problems=problems,
            )
        )

    if model_shadow.get("status") != AGENT_OS_READINESS_CHECK_PASSED:
        gaps.append(
            _frontier_evidence_gap(
                gate="model_shadow_real_manifest",
                label="real shadow evidence",
                required=AGENT_MODEL_SHADOW_REQUIRED_EVIDENCE_MODE,
                actual=str(model_shadow.get("evidence_mode") or "missing"),
                path=str(model_shadow.get("path") or AGENT_MODEL_SHADOW_EVIDENCE_SCORECARD_REL_PATH),
                next_action=model_evidence_next_action,
                problems=model_shadow.get("contract_problems") or (),
            )
        )
    if model_tournament.get("status") != AGENT_OS_READINESS_CHECK_PASSED:
        gaps.append(
            _frontier_evidence_gap(
                gate="model_tournament_real_artifacts",
                label="real tournament artifacts",
                required=AGENT_MODEL_CANDIDATE_TOURNAMENT_REQUIRED_EVIDENCE_MODE,
                actual=str(model_tournament.get("evidence_mode") or "missing"),
                path=str(
                    model_tournament.get("path")
                    or AGENT_MODEL_CANDIDATE_TOURNAMENT_SCORECARD_REL_PATH
                ),
                next_action=model_evidence_next_action,
                problems=model_tournament.get("contract_problems") or (),
            )
        )
    model_evidence_blocked = (
        model_shadow.get("status") != AGENT_OS_READINESS_CHECK_PASSED
        or model_tournament.get("status") != AGENT_OS_READINESS_CHECK_PASSED
    )
    if local_model_candidate_run.get("failed") and model_evidence_blocked:
        failure_reason = str(local_model_candidate_run.get("failure_reason") or "").strip()
        failed_case = str(local_model_candidate_run.get("failed_case_id") or "").strip()
        actual = failure_reason or str(local_model_candidate_run.get("status") or "failed")
        if failed_case and failed_case not in actual:
            actual = f"{failed_case}: {actual}"
        timeout_salvaged_count = int(
            local_model_candidate_run.get("timeout_salvaged_case_count") or 0
        )
        if timeout_salvaged_count:
            actual += f"; partial-timeout salvage recorded for {timeout_salvaged_count} case(s)"
        gaps.append(
            _frontier_evidence_gap(
                gate="local_model_candidate_run",
                label="local model candidate diagnostics",
                required="failed local-model suite has a case-scoped retry/import recovery route",
                actual=actual,
                path=str(
                    local_model_candidate_run.get("diagnostics")
                    or local_model_candidate_run.get("path")
                    or AGENT_LOCAL_MODEL_CANDIDATE_RUN_REL_PATH
                ),
                next_action=str(
                    local_model_candidate_run.get("next_action")
                    or AGENT_LOCAL_MODEL_CANDIDATE_RUN_COMMAND
                ),
                problems=([failure_reason] if failure_reason else ()),
            )
        )
    if hosted_pr_repair.get("status") != AGENT_OS_READINESS_CHECK_PASSED:
        hosted_actual = str(hosted_pr_repair.get("evidence_mode") or "missing")
        hosted_metadata = hosted_pr_repair.get("metadata_values")
        if isinstance(hosted_metadata, Mapping):
            promotion_eligible = str(hosted_metadata.get("promotion eligible") or "").strip()
            if promotion_eligible and promotion_eligible != "true":
                hosted_actual = f"{hosted_actual}; promotion eligible {promotion_eligible}"
        gaps.append(
            _frontier_evidence_gap(
                gate="hosted_pr_repair_real_inventory",
                label="real PR repair inventory",
                required=AGENT_HOSTED_PR_REPAIR_REQUIRED_EVIDENCE_MODE,
                actual=hosted_actual,
                path=str(hosted_pr_repair.get("path") or AGENT_HOSTED_PR_REPAIR_SCORECARD_REL_PATH),
                next_action=(
                    "Collect hosted PR repair artifacts with review-thread transcripts, "
                    "publication proof, and current-head check receipts."
                ),
                problems=hosted_pr_repair.get("contract_problems") or (),
            )
        )
    return gaps


def _frontier_model_evidence_collection_lines() -> list[str]:
    raw_root = AGENT_FRONTIER_MODEL_EVIDENCE_RAW_SOURCES_REL_PATH
    output_root = AGENT_FRONTIER_MODEL_EVIDENCE_OUTPUT_ROOT_REL_PATH
    lines = [
        "",
        "Source-specific model collection setup:",
        f"- Prompt pack manifest: {AGENT_FRONTIER_PROMPT_PACK_MANIFEST_REL_PATH}",
        f"- Raw source root: {raw_root}",
        "- Required source directories: codex, claude, local_model.",
    ]
    for source_kind in AGENT_FRONTIER_MODEL_EVIDENCE_SOURCE_KINDS:
        source_root = f"{raw_root}/{source_kind}"
        lines.append(
            f"- {source_kind}: {source_root}/metadata.json, "
            f"{source_root}/prompt_pack.md, {source_root}/transcript.jsonl, "
            f"and {source_root}/raw/"
        )
    lines.extend(
        [
            "",
            "Commands to close model evidence gaps:",
            f"- Prepare intake folders safely: {AGENT_FRONTIER_MODEL_EVIDENCE_SETUP_COMMAND}",
            f"- Build copy-ready Codex/Claude collection packets: {AGENT_FRONTIER_SOURCE_COLLECTION_PACKET_COMMAND}",
            f"- Record Codex/Claude/local source evidence safely: {AGENT_FRONTIER_SOURCE_EVIDENCE_RECORD_COMMAND}",
            f"- Record all-cases Codex/Claude/local source evidence safely: {AGENT_FRONTIER_SOURCE_EVIDENCE_RECORD_ALL_CASES_COMMAND}",
            f"- Run local-model candidate suite collection: {AGENT_LOCAL_MODEL_CANDIDATE_RUN_COMMAND}",
            f"- Record local-model evidence safely: {AGENT_LOCAL_MODEL_EVIDENCE_RECORD_COMMAND}",
            "- Validate source-specific prompt packs: python scripts/autopilot_frontier_prompt_pack_bundle.py --validate --json",
            (
                "- Ingest real source drops and publish scorecards: "
                "python scripts/autopilot_frontier_model_evidence_intake.py "
                f"--input-root {raw_root} --output-root {output_root} "
                "--publish-scorecards --json"
            ),
            (
                "- Verify real shadow manifests: "
                "python scripts/autopilot_model_shadow_evidence_benchmark.py "
                f"--manifest-dir {output_root}/manifests --no-write --json"
            ),
            (
                "- Verify real tournament artifacts: "
                "python scripts/autopilot_model_candidate_tournament_benchmark.py "
                f"--drop-dir {output_root}/collected --require-provenance "
                "--no-write --json"
            ),
        ]
    )
    return lines


def _frontier_source_all_cases_response_rel_path(source_kind: str) -> str:
    return (
        f"{AGENT_FRONTIER_MODEL_EVIDENCE_COLLECTION_PACKETS_REL_PATH}/"
        f"{source_kind}_all_cases_response.txt"
    )


def _frontier_source_single_case_response_rel_path(source_kind: str) -> str:
    return (
        f"{AGENT_FRONTIER_MODEL_EVIDENCE_COLLECTION_PACKETS_REL_PATH}/"
        f"{source_kind}_single_case_response.txt"
    )


def _frontier_source_response_import_command(source_kind: str) -> str:
    return (
        "python scripts/autopilot_frontier_source_evidence_recorder.py "
        f"--source-kind {source_kind} "
        f"--case-id {AGENT_FRONTIER_RESPONSE_IMPORT_CASE_ID} "
        f"--response {_frontier_source_single_case_response_rel_path(source_kind)} "
        f"--run-id <real-{source_kind}-run-id> "
        f"--source-command <exact-{source_kind}-command-or-session-export> --json"
    )


def _frontier_source_response_import_all_cases_command(
    source_kind: str,
    *,
    no_write: bool = False,
) -> str:
    command = (
        "python scripts/autopilot_frontier_source_evidence_recorder.py "
        f"--source-kind {source_kind} "
        "--all-cases "
        f"--response {_frontier_source_all_cases_response_rel_path(source_kind)} "
        f"--run-id <real-{source_kind}-run-id> "
        f"--source-command <exact-{source_kind}-command-or-session-export> --json"
    )
    if no_write:
        command += " --no-write"
    return command


def _frontier_source_collection_packet_command(source_kind: str) -> str:
    return (
        "python scripts/autopilot_frontier_source_collection_packet.py "
        f"--source-kind {source_kind} --json"
    )


def _frontier_preflight_report_path(runtime_path: Path) -> tuple[Path, str] | None:
    candidates = [
        (runtime_path / Path(rel_path), rel_path)
        for rel_path in (
            AGENT_FRONTIER_EVIDENCE_PREFLIGHT_LIVE_REL_PATH,
            AGENT_FRONTIER_EVIDENCE_PREFLIGHT_REL_PATH,
        )
    ]
    existing = [(path, rel_path) for path, rel_path in candidates if path.is_file()]
    if not existing:
        return None
    return max(
        existing,
        key=lambda item: item[0].stat().st_mtime if item[0].exists() else 0,
    )


def _frontier_preflight_blocker_source(blocker_id: str) -> str:
    normalized = blocker_id.strip().lower()
    if normalized in {"codex_cli_available", "codex_cli_live_probe"}:
        return "codex"
    if normalized in {"claude_cli_available", "claude_opus48_live_probe"}:
        return "claude"
    return ""


def _frontier_preflight_recovery_route(
    *,
    source_kind: str,
    blocker_id: str,
) -> dict[str, Any]:
    cli_blocked = blocker_id.endswith("_available")
    reason = (
        f"{source_kind} CLI is unavailable; collect from a trusted UI/API "
        "session and import the saved response instead."
        if cli_blocked
        else (
            f"{source_kind} automated live probe is not usable right now; "
            "a saved hosted response can still be transcript-bound and "
            "validated by the frontier source recorder."
        )
    )
    return {
        "source_kind": source_kind,
        "blocker_id": blocker_id,
        "status": "available",
        "action_label": f"Import saved {source_kind} response",
        "reason": reason,
        "collection_packet_command": _frontier_source_collection_packet_command(source_kind),
        "response_staging_file": _frontier_source_all_cases_response_rel_path(source_kind),
        "dry_run_response_import_command": _frontier_source_response_import_all_cases_command(
            source_kind,
            no_write=True,
        ),
        "response_import_command": _frontier_source_response_import_all_cases_command(source_kind),
        "all_cases_response_import_command": _frontier_source_response_import_all_cases_command(source_kind),
        "single_case_response_import_command": _frontier_source_response_import_command(source_kind),
        "permission_boundary": (
            "collection and evidence import only; does not run models, edit source/tests, "
            "use git/PR tools, restart runtime, deploy, or touch live trading"
        ),
    }


def _frontier_evidence_preflight_status(runtime_path: Path) -> dict[str, Any]:
    report = _frontier_preflight_report_path(runtime_path)
    if report is None:
        return {
            "status": "missing",
            "ready": False,
            "path": AGENT_FRONTIER_EVIDENCE_PREFLIGHT_LIVE_REL_PATH,
            "generated_utc": "",
            "check_count": 0,
            "blocker_count": 0,
            "blocker_ids": [],
            "recovery_route_count": 0,
            "recovery_routes": [],
            "next_action": (
                "Run scripts/autopilot_frontier_evidence_preflight.py "
                "--live-model-probes --json"
            ),
            "permission_boundary": (
                "evidence readiness checks only; no source/runtime/git/PR/live action"
            ),
        }

    path, rel_path = report
    metadata = _scorecard_metadata(path)
    rows = [
        row
        for row in _scorecard_table_rows(path)
        if _scorecard_row_status(row)
        in {AGENT_OS_READINESS_CHECK_PASSED, AGENT_OS_READINESS_CHECK_WARNING}
    ]
    warning_rows = [
        row
        for row in rows
        if _scorecard_row_status(row) != AGENT_OS_READINESS_CHECK_PASSED
    ]
    blocker_ids = [
        str(row.get("check") or "").strip()
        for row in warning_rows
        if str(row.get("check") or "").strip()
    ]
    routes_by_blocker: dict[str, dict[str, Any]] = {}
    for blocker_id in blocker_ids:
        source_kind = _frontier_preflight_blocker_source(blocker_id)
        if not source_kind:
            continue
        routes_by_blocker.setdefault(
            blocker_id,
            _frontier_preflight_recovery_route(
                source_kind=source_kind,
                blocker_id=blocker_id,
            ),
        )
    recovery_routes = list(routes_by_blocker.values())
    status = _scorecard_text(metadata, "status", "missing").lower()
    check_count = _scorecard_int(metadata, "checks", len(rows))
    blocker_count = _scorecard_int(metadata, "blockers", len(warning_rows))
    next_action = ""
    if recovery_routes:
        next_action = str(recovery_routes[0].get("response_import_command") or "")
    elif warning_rows:
        next_action = str(warning_rows[0].get("next action") or "")
    if not next_action:
        next_action = "none" if status == AGENT_OS_READINESS_CHECK_PASSED else (
            "Run scripts/autopilot_frontier_evidence_preflight.py "
            "--live-model-probes --json"
        )
    return {
        "status": status,
        "ready": status == AGENT_OS_READINESS_CHECK_PASSED and not warning_rows,
        "path": rel_path,
        "generated_utc": _scorecard_text(metadata, "generated utc"),
        "check_count": check_count,
        "blocker_count": blocker_count,
        "blocker_ids": blocker_ids,
        "recovery_route_count": len(recovery_routes),
        "recovery_routes": recovery_routes,
        "next_action": next_action,
        "permission_boundary": (
            "evidence readiness checks and imports only; no source/runtime/git/PR/live action"
        ),
    }


def _frontier_preflight_recovery_lines(preflight: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(preflight, Mapping):
        return []
    raw_routes = preflight.get("recovery_routes")
    if not isinstance(raw_routes, Iterable) or isinstance(raw_routes, (str, bytes, Mapping)):
        return []
    routes = [route for route in raw_routes if isinstance(route, Mapping)]
    if not routes:
        return []
    lines = [
        "",
        "Hosted-source preflight recovery routes:",
        f"- Preflight report: {preflight.get('path') or AGENT_FRONTIER_EVIDENCE_PREFLIGHT_LIVE_REL_PATH}",
    ]
    for route in routes:
        source_kind = str(route.get("source_kind") or "hosted source")
        blocker_id = str(route.get("blocker_id") or "unknown blocker")
        action_label = str(route.get("action_label") or "Import saved response")
        response_command = str(route.get("response_import_command") or "")
        dry_run_command = str(route.get("dry_run_response_import_command") or "")
        fallback_command = str(route.get("single_case_response_import_command") or "")
        collection_command = str(route.get("collection_packet_command") or "")
        staging_file = str(route.get("response_staging_file") or "")
        boundary = str(route.get("permission_boundary") or "")
        reason = str(route.get("reason") or "")
        lines.append(f"- {action_label} for {source_kind} blocker {blocker_id}.")
        if reason:
            lines.append(f"- Reason: {reason}")
        if collection_command:
            lines.append(f"- Collection packet: {collection_command}")
        if staging_file:
            lines.append(f"- Save all-cases response to: {staging_file}")
        if dry_run_command:
            lines.append(f"- Dry-run response import: {dry_run_command}")
        if response_command:
            lines.append(f"- All-cases response import: {response_command}")
        if fallback_command:
            lines.append(f"- Single-case fallback: {fallback_command}")
        if boundary:
            lines.append(f"- Boundary: {boundary}")
    return lines


def _relative_to_runtime(path: Path, runtime_path: Path) -> str:
    try:
        return path.relative_to(runtime_path).as_posix()
    except ValueError:
        return path.as_posix()


def _markdown_section_bullets(text: str, heading: str) -> list[str]:
    capture = False
    bullets: list[str] = []
    heading_line = f"## {heading}".strip().lower()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.lower() == heading_line:
            capture = True
            continue
        if capture and line.startswith("## "):
            break
        if capture and line.startswith("- "):
            bullet = line[2:].strip()
            if bullet:
                bullets.append(bullet)
    return bullets


def _hosted_pr_repair_candidate_reports(
    runtime_path: Path,
    *,
    limit: int = 3,
) -> list[dict[str, Any]]:
    agentops_root = runtime_path / "project_ws" / "AgentOps"
    if not agentops_root.is_dir():
        return []
    candidates: list[dict[str, Any]] = []
    for path in agentops_root.glob(AGENT_HOSTED_PR_REPAIR_REPORT_GLOB):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            modified_at = path.stat().st_mtime
        except OSError:
            continue
        metadata = _scorecard_metadata(path)
        candidates.append(
            {
                "path": _relative_to_runtime(path, runtime_path),
                "generated_utc": _scorecard_text(metadata, "generated utc"),
                "updated_utc": _scorecard_text(metadata, "updated utc"),
                "pr_url": _scorecard_text(metadata, "pr"),
                "branch": _scorecard_text(metadata, "branch"),
                "head_sha_inspected": _scorecard_text(metadata, "head sha inspected"),
                "current_head_sha_observed": _scorecard_text(metadata, "current head sha observed"),
                "hosted_run_inspected": _scorecard_text(metadata, "hosted run inspected"),
                "current_hosted_green_run_observed": _scorecard_text(
                    metadata,
                    "current hosted green run observed",
                ),
                "evidence_status": _scorecard_text(metadata, "evidence status"),
                "promotion_status": _scorecard_text(metadata, "promotion status"),
                "missing_evidence": _markdown_section_bullets(text, "Remaining Hosted Evidence"),
                "modified_at": modified_at,
            }
        )
    candidates.sort(
        key=lambda item: (
            str(item.get("updated_utc") or item.get("generated_utc") or ""),
            float(item.get("modified_at") or 0),
        ),
        reverse=True,
    )
    for candidate in candidates:
        candidate.pop("modified_at", None)
    return candidates[: max(1, int(limit or 1))]


def _hosted_pr_repair_collection_packet_command(candidate_path: str = "") -> str:
    if candidate_path:
        return (
            "python scripts/autopilot_hosted_pr_repair_collection_packet.py "
            f"--candidate-report {candidate_path} --json"
        )
    return AGENT_HOSTED_PR_REPAIR_COLLECTION_PACKET_COMMAND


def _hosted_pr_repair_evidence_collector_command(candidate_path: str = "") -> str:
    if candidate_path:
        return (
            "python scripts/autopilot_hosted_pr_repair_evidence_collector.py "
            f"--candidate-report {candidate_path} --json"
        )
    return AGENT_HOSTED_PR_REPAIR_EVIDENCE_COLLECTOR_COMMAND


def _hosted_pr_repair_artifact_assembler_command(candidate_path: str = "") -> str:
    if candidate_path:
        return (
            "python scripts/autopilot_hosted_pr_repair_artifact_assembler.py "
            f"--candidate-report {candidate_path} --json"
        )
    return AGENT_HOSTED_PR_REPAIR_ARTIFACT_ASSEMBLER_COMMAND


def _hosted_pr_repair_candidate_status(runtime_path: Path) -> dict[str, Any]:
    candidates = _hosted_pr_repair_candidate_reports(runtime_path)
    if not candidates:
        return {
            "status": "missing",
            "ready": False,
            "candidate_count": 0,
            "reports": [],
            "latest": {},
            "next_action": (
                "Collect a real hosted PR repair report with PR URL, repaired head, hosted "
                "check result, publication transcript, and current-head check receipt."
            ),
            "collection_packet_command": AGENT_HOSTED_PR_REPAIR_COLLECTION_PACKET_COMMAND,
            "collection_packet_action_label": "Build hosted PR repair collection packet",
            "collection_packet_safe": True,
            "evidence_collector_command": AGENT_HOSTED_PR_REPAIR_EVIDENCE_COLLECTOR_COMMAND,
            "evidence_collector_action_label": "Collect hosted PR repair evidence",
            "evidence_collector_safe": True,
            "artifact_assembler_command": AGENT_HOSTED_PR_REPAIR_ARTIFACT_ASSEMBLER_COMMAND,
            "artifact_assembler_action_label": "Assemble hosted PR repair artifact",
            "artifact_assembler_safe": True,
            "validation_command": AGENT_HOSTED_PR_REPAIR_ARTIFACT_VALIDATE_COMMAND,
            "permission_boundary": (
                "report inspection and evidence validation only; no git/PR mutation, "
                "runtime restart, deploy, database, broker, or live-trading action"
            ),
        }
    latest = candidates[0]
    latest_path = str(latest.get("path") or "")
    missing = latest.get("missing_evidence")
    missing_count = len(missing) if isinstance(missing, list) else 0
    return {
        "status": "candidate_reports_present",
        "ready": False,
        "candidate_count": len(candidates),
        "reports": candidates,
        "latest": latest,
        "latest_path": latest_path,
        "latest_pr_url": latest.get("pr_url") or "",
        "missing_evidence_count": missing_count,
        "missing_evidence": missing if isinstance(missing, list) else [],
        "next_action": (
            "Promote the latest hosted PR repair candidate only after collecting "
            "transcript-bound hosted PR repair artifacts: review-thread, publication, "
            "post-repair PR status, and current-head check-success transcripts, then "
            "replaying them through the hosted PR repair artifact validator."
        ),
        "collection_packet_command": _hosted_pr_repair_collection_packet_command(latest_path),
        "collection_packet_action_label": "Build hosted PR repair collection packet",
        "collection_packet_safe": True,
        "evidence_collector_command": _hosted_pr_repair_evidence_collector_command(latest_path),
        "evidence_collector_action_label": "Collect hosted PR repair evidence",
        "evidence_collector_safe": True,
        "artifact_assembler_command": _hosted_pr_repair_artifact_assembler_command(latest_path),
        "artifact_assembler_action_label": "Assemble hosted PR repair artifact",
        "artifact_assembler_safe": True,
        "validation_command": AGENT_HOSTED_PR_REPAIR_ARTIFACT_VALIDATE_COMMAND,
        "permission_boundary": (
            "report inspection and evidence validation only; no git/PR mutation, "
            "runtime restart, deploy, database, broker, or live-trading action"
        ),
    }


def _hosted_pr_repair_candidate_lines(
    candidates: Mapping[str, Any] | None,
) -> list[str]:
    if not isinstance(candidates, Mapping):
        return []
    latest = candidates.get("latest")
    if not isinstance(latest, Mapping) or not latest:
        return []
    lines = [
        "",
        "Hosted PR repair candidate reports:",
        f"- Latest report: {latest.get('path') or candidates.get('latest_path') or 'missing'}",
        f"- PR: {latest.get('pr_url') or 'missing'}",
        f"- Evidence status: {latest.get('evidence_status') or 'missing'}",
        f"- Promotion status: {latest.get('promotion_status') or 'missing'}",
        f"- Current head observed: {latest.get('current_head_sha_observed') or 'missing'}",
        f"- Current hosted green run: {latest.get('current_hosted_green_run_observed') or 'missing'}",
    ]
    missing = latest.get("missing_evidence")
    if isinstance(missing, list) and missing:
        lines.append("- Missing evidence before real_inventory promotion:")
        for item in missing[:4]:
            lines.append(f"  - {item}")
    lines.extend(
        [
            f"- Collection packet: {candidates.get('collection_packet_command') or AGENT_HOSTED_PR_REPAIR_COLLECTION_PACKET_COMMAND}",
            f"- Evidence collector: {candidates.get('evidence_collector_command') or AGENT_HOSTED_PR_REPAIR_EVIDENCE_COLLECTOR_COMMAND}",
            f"- Artifact assembler: {candidates.get('artifact_assembler_command') or AGENT_HOSTED_PR_REPAIR_ARTIFACT_ASSEMBLER_COMMAND}",
            f"- Validation command: {candidates.get('validation_command') or AGENT_HOSTED_PR_REPAIR_ARTIFACT_VALIDATE_COMMAND}",
            f"- Next action: {candidates.get('next_action') or 'Collect transcript-bound hosted PR repair artifacts.'}",
            f"- Boundary: {candidates.get('permission_boundary') or 'evidence validation only'}",
        ]
    )
    return lines


def _frontier_model_evidence_intake_status(runtime_path: Path) -> dict[str, Any]:
    raw_root_rel = AGENT_FRONTIER_MODEL_EVIDENCE_RAW_SOURCES_REL_PATH
    raw_root = runtime_path / Path(raw_root_rel)
    output_root_rel = AGENT_FRONTIER_MODEL_EVIDENCE_OUTPUT_ROOT_REL_PATH
    manifest_rel = AGENT_FRONTIER_PROMPT_PACK_MANIFEST_REL_PATH
    local_model_candidate_run = _local_model_candidate_run_status(runtime_path)
    frontier_evidence_preflight = _frontier_evidence_preflight_status(runtime_path)
    preflight_recovery_by_source = {
        str(route.get("source_kind") or "").strip(): route
        for route in frontier_evidence_preflight.get("recovery_routes") or []
        if isinstance(route, Mapping)
        and str(route.get("source_kind") or "").strip()
    }
    source_results: list[dict[str, Any]] = []
    for source_kind in AGENT_FRONTIER_MODEL_EVIDENCE_SOURCE_KINDS:
        source_rel = f"{raw_root_rel}/{source_kind}"
        source_path = runtime_path / Path(source_rel)
        missing_files: list[str] = []
        present_files: list[str] = []
        for filename in AGENT_FRONTIER_MODEL_EVIDENCE_REQUIRED_SOURCE_FILES:
            rel_file = f"{source_rel}/{filename}"
            if (runtime_path / Path(rel_file)).is_file():
                present_files.append(rel_file)
            else:
                missing_files.append(rel_file)
        raw_dir = source_path / "raw"
        raw_drop_count = 0
        if raw_dir.is_dir():
            try:
                raw_drop_count = sum(1 for path in raw_dir.rglob("*") if path.is_file())
            except OSError:
                raw_drop_count = 0
        if raw_drop_count <= 0:
            missing_files.append(f"{source_rel}/raw/*")
        status = (
            "ready"
            if not missing_files
            else "missing"
            if not source_path.exists()
            else "partial"
        )
        recovery_route = (
            preflight_recovery_by_source.get(source_kind)
            if status != "ready"
            else None
        )
        recovery_action_label = (
            str(recovery_route.get("action_label") or "").strip()
            if recovery_route
            else ""
        )
        recovery_staging_file = (
            str(recovery_route.get("response_staging_file") or "").strip()
            if recovery_route
            else ""
        )
        recovery_dry_run_command = (
            str(recovery_route.get("dry_run_response_import_command") or "").strip()
            if recovery_route
            else ""
        )
        recovery_all_cases_command = (
            str(recovery_route.get("all_cases_response_import_command") or "").strip()
            if recovery_route
            else ""
        )
        recovery_single_case_command = (
            str(recovery_route.get("single_case_response_import_command") or "").strip()
            if recovery_route
            else ""
        )
        recovery_boundary = (
            str(recovery_route.get("permission_boundary") or "").strip()
            if recovery_route
            else ""
        )
        recovery_validation_command = (
            "python scripts/autopilot_frontier_model_evidence_intake.py "
            f"--input-root {raw_root_rel} --allow-partial --json --no-write"
            if recovery_route
            else ""
        )
        recovery_publish_command = (
            "python scripts/autopilot_frontier_model_evidence_intake.py "
            f"--input-root {raw_root_rel} --publish-scorecards --json"
            if recovery_route
            else ""
        )
        if status == "ready":
            source_next_action = "none"
        elif recovery_route:
            source_next_action = (
                f"Preflight recovery: {recovery_action_label or f'import saved {source_kind} response'}. "
                f"Save all-cases response to: {recovery_staging_file}. "
                f"Dry-run import first: {recovery_dry_run_command}. "
                f"All-cases import: {recovery_all_cases_command}. "
                f"Single-case fallback: {recovery_single_case_command}. "
                f"After import validation: {recovery_validation_command}. "
                f"Publish only when all sources are ready: {recovery_publish_command}. "
                f"Boundary: {recovery_boundary or 'evidence import only'}."
            )
        else:
            source_next_action = (
                f"Populate {source_rel} with metadata.json, prompt_pack.md, "
                "transcript.jsonl, and raw candidate artifacts."
            )
        source_results.append(
            {
                "source_kind": source_kind,
                "status": status,
                "path": source_rel,
                "present_files": present_files,
                "missing_files": missing_files,
                "raw_drop_count": raw_drop_count,
                "next_action": source_next_action,
                "preflight_recovery_route": recovery_route or {},
                "preflight_recovery_action_label": recovery_action_label,
                "preflight_recovery_response_staging_file": recovery_staging_file,
                "preflight_recovery_dry_run_command": recovery_dry_run_command,
                "preflight_recovery_all_cases_command": recovery_all_cases_command,
                "preflight_recovery_single_case_command": recovery_single_case_command,
                "preflight_recovery_boundary": recovery_boundary,
                "preflight_recovery_validation_command": recovery_validation_command,
                "preflight_recovery_publish_command": recovery_publish_command,
            }
        )
    required_count = len(AGENT_FRONTIER_MODEL_EVIDENCE_SOURCE_KINDS)
    ready_count = sum(1 for source in source_results if source["status"] == "ready")
    prepared_count = sum(
        1
        for source in source_results
        if f"{source['path']}/prompt_pack.md" not in source["missing_files"]
    )
    missing_count = required_count - ready_count
    manifest_present = (runtime_path / Path(manifest_rel)).is_file()
    status = (
        "ready"
        if ready_count == required_count and manifest_present
        else "missing"
        if not raw_root.exists()
        else "partial"
    )
    if not manifest_present:
        next_action = (
            "Validate or regenerate source-specific prompt packs before collecting model drops."
        )
    elif not raw_root.exists():
        next_action = (
            "Prepare frontier source folders with "
            f"{AGENT_FRONTIER_MODEL_EVIDENCE_SETUP_COMMAND}; then record real "
            "metadata.json, transcript.jsonl, and raw candidate artifacts."
        )
    elif prepared_count == required_count and missing_count:
        missing_sources = ", ".join(
            source["source_kind"]
            for source in source_results
            if source["status"] != "ready"
        )
        recovery_sources = [
            source
            for source in source_results
            if source["status"] != "ready"
            and source.get("preflight_recovery_all_cases_command")
        ]
        recovery_hint = ""
        if recovery_sources:
            first_recovery = recovery_sources[0]
            recovery_hint = (
                f"Preflight recovery available for {first_recovery['source_kind']}: "
                f"save response to {first_recovery['preflight_recovery_response_staging_file']}; "
                f"dry-run {first_recovery['preflight_recovery_dry_run_command']}; "
                f"{first_recovery['preflight_recovery_all_cases_command']}. "
                f"Validate after import with {first_recovery['preflight_recovery_validation_command']}. "
            )
        local_model_missing = any(
            source["source_kind"] == "local_model" and source["status"] != "ready"
            for source in source_results
        )
        local_hint = (
            f" Use {AGENT_LOCAL_MODEL_CANDIDATE_RUN_COMMAND} to generate and record a compact "
            f"local_model candidate, or {AGENT_LOCAL_MODEL_EVIDENCE_RECORD_COMMAND} to import "
            "existing local_model drops."
            if local_model_missing
            else ""
        )
        next_action = (
            recovery_hint
            + (
            "Record real metadata.json, transcript.jsonl, and raw candidate "
            f"artifacts for prepared frontier sources: {missing_sources}. "
            f"Use {AGENT_FRONTIER_SOURCE_COLLECTION_PACKET_COMMAND} to generate "
            "copy-ready hosted-source collection packets. "
            f"Use {AGENT_FRONTIER_SOURCE_EVIDENCE_RECORD_ALL_CASES_COMMAND} to import "
            "complete hosted Codex/Claude/source drops; use "
            f"{AGENT_FRONTIER_SOURCE_EVIDENCE_RECORD_COMMAND} only when a hosted source "
            f"produced one case.{local_hint}"
            )
        )
    elif missing_count:
        missing_sources = ", ".join(
            source["source_kind"]
            for source in source_results
            if source["status"] != "ready"
        )
        next_action = f"Populate missing frontier source bundles: {missing_sources}."
    else:
        next_action = (
            "Run scripts/autopilot_frontier_model_evidence_intake.py with "
            "--publish-scorecards to close real model evidence gates."
        )
    return {
        "status": status,
        "ready": status == "ready",
        "required_source_count": required_count,
        "ready_source_count": ready_count,
        "prepared_source_count": prepared_count,
        "missing_source_count": missing_count,
        "prompt_pack_manifest": manifest_rel,
        "prompt_pack_manifest_present": manifest_present,
        "raw_source_root": raw_root_rel,
        "output_root": output_root_rel,
        "setup_command": AGENT_FRONTIER_MODEL_EVIDENCE_SETUP_COMMAND,
        "setup_action_label": "Prepare frontier intake folders",
        "setup_safe": True,
        "frontier_source_collection_packet_command": AGENT_FRONTIER_SOURCE_COLLECTION_PACKET_COMMAND,
        "frontier_source_collection_packet_action_label": "Build source collection packets",
        "frontier_source_collection_packet_safe": True,
        "frontier_source_record_command": AGENT_FRONTIER_SOURCE_EVIDENCE_RECORD_COMMAND,
        "frontier_source_record_action_label": "Record frontier source evidence",
        "frontier_source_record_safe": True,
        "frontier_source_record_all_cases_command": AGENT_FRONTIER_SOURCE_EVIDENCE_RECORD_ALL_CASES_COMMAND,
        "frontier_source_record_all_cases_action_label": "Record all-cases frontier source evidence",
        "frontier_source_record_all_cases_safe": True,
        "local_model_candidate_run_command": AGENT_LOCAL_MODEL_CANDIDATE_RUN_COMMAND,
        "local_model_candidate_run_action_label": "Run local-model candidate suite",
        "local_model_candidate_run_safe": True,
        "local_model_candidate_run": local_model_candidate_run,
        "frontier_evidence_preflight": frontier_evidence_preflight,
        "frontier_preflight_recovery_routes": frontier_evidence_preflight[
            "recovery_routes"
        ],
        "frontier_preflight_recovery_route_count": frontier_evidence_preflight[
            "recovery_route_count"
        ],
        "local_model_record_command": AGENT_LOCAL_MODEL_EVIDENCE_RECORD_COMMAND,
        "local_model_record_action_label": "Record local-model evidence",
        "local_model_record_safe": True,
        "sources": source_results,
        "next_action": next_action,
        "permission_boundary": (
            "evidence collection and verification only; no source/runtime/git/PR/live action"
        ),
    }


def _frontier_evidence_handoff_copy(
    *,
    score: int,
    scenario_count: int,
    pass_rate: str,
    effective_pass_rate: str,
    profile: str,
    promotion_status: str,
    selected_scenarios_status: str,
    generated_utc: str,
    gaps: Sequence[Mapping[str, Any]],
    preflight: Mapping[str, Any] | None = None,
    hosted_pr_candidates: Mapping[str, Any] | None = None,
) -> str:
    preflight_recovery_lines = _frontier_preflight_recovery_lines(preflight)
    hosted_pr_candidate_lines = _hosted_pr_repair_candidate_lines(hosted_pr_candidates)
    if not gaps and not preflight_recovery_lines and not hosted_pr_candidate_lines:
        return ""
    lines = [
        "Project Autopilot frontier evidence proof packet",
        "Purpose: collect or verify the real evidence blocking Codex/Claude-class promotion readiness.",
        "Do not summarize this as promotion-ready until every listed proof gap has fresh evidence.",
        "",
        "Current benchmark evidence:",
        f"- Profile: {profile or 'missing'}",
        f"- Promotion status: {promotion_status or 'missing'}",
        f"- Selected scenarios status: {selected_scenarios_status or 'missing'}",
        f"- Score: {score}/{AGENT_CODING_BENCHMARK_TARGET_SCORE}",
        f"- Scenarios: {scenario_count}",
        f"- Pass rate: {effective_pass_rate or pass_rate or 'missing'}"
        + (
            f" (raw {pass_rate})"
            if pass_rate and effective_pass_rate and pass_rate != effective_pass_rate
            else ""
        ),
        f"- Generated UTC: {generated_utc or 'missing'}",
        "",
    ]
    if gaps:
        lines.append("Proof gaps to close:")
        for index, gap in enumerate(gaps, start=1):
            problems = [
                str(problem).strip()
                for problem in gap.get("problems") or []
                if str(problem).strip()
            ]
            lines.extend(
                [
                    f"{index}. {gap.get('label') or gap.get('gate') or 'frontier evidence'}",
                    f"   Required: {gap.get('required') or 'missing'}",
                    f"   Actual: {gap.get('actual') or 'missing'}",
                    f"   Evidence path: {gap.get('path') or 'missing'}",
                    f"   Next action: {gap.get('next_action') or 'Collect current proof evidence.'}",
                ]
            )
            if problems:
                lines.append(f"   Contract problem: {problems[0]}")
                for problem in problems[1:3]:
                    lines.append(f"   Additional problem: {problem}")
    else:
        lines.extend(
            [
                "Proof gaps to close:",
                "1. No benchmark proof gaps are currently recorded; preflight recovery is listed below.",
            ]
        )
    if any(
        str(gap.get("gate") or "").startswith("model_")
        for gap in gaps
    ):
        lines.extend(_frontier_model_evidence_collection_lines())
    lines.extend(preflight_recovery_lines)
    lines.extend(hosted_pr_candidate_lines)
    lines.extend(
        [
            "",
            "Success criteria:",
            (
                "- Source freshness gap: generate "
                f"{AGENT_SOURCE_CHURN_DIAGNOSTICS_REL_PATH}, rerun the full coding "
                "benchmark after source/test churn settles, and prove the scorecard is current."
            ),
            "- Model evidence gaps: provide transcript-verified Codex, Claude, and local-model artifacts with required evidence modes.",
            "- Hosted PR repair gap: provide real inventory with review-thread transcripts, publication proof, post-repair PR status, and current-head check receipts.",
            "- Report the exact regenerated scorecard/manifest paths and hashes; do not rely on self-test fixtures.",
            "",
            "Permission boundary: evidence collection and verification only. This packet does not authorize source/test edits, runtime restart, Docker, database/migration, broker/API, PR mutation, commit, push, merge, release, deploy, route/model changes, or live-trading behavior.",
        ]
    )
    return "\n".join(lines)


def _agent_coding_benchmark_signal(runtime_path: Path | None) -> dict[str, Any]:
    runtime_path = runtime_path or Path.cwd()
    scorecard_path = runtime_path / Path(AGENT_CODING_BENCHMARK_SCORECARD_REL_PATH)
    metadata = _scorecard_metadata(scorecard_path)
    frontier_model_evidence_intake = _frontier_model_evidence_intake_status(runtime_path)
    frontier_evidence_preflight = _frontier_evidence_preflight_status(runtime_path)
    hosted_pr_repair_candidates = _hosted_pr_repair_candidate_status(runtime_path)
    source_churn_diagnostics = _source_churn_diagnostics_summary(runtime_path)
    if not metadata:
        missing_handoff_lines = [
            "Project Autopilot frontier evidence proof packet",
            "Purpose: generate the missing coding benchmark scorecard before judging Codex/Claude-class promotion readiness.",
            f"Evidence path: {AGENT_CODING_BENCHMARK_SCORECARD_REL_PATH}",
            "Next action: Generate the coding benchmark scorecard before judging frontier readiness.",
        ]
        missing_handoff_lines.extend(_frontier_preflight_recovery_lines(frontier_evidence_preflight))
        missing_handoff_lines.extend(_hosted_pr_repair_candidate_lines(hosted_pr_repair_candidates))
        missing_handoff_lines.append(
            "Permission boundary: evidence collection and verification only. This packet does not authorize source/test edits, runtime restart, Docker, database/migration, broker/API, PR mutation, commit, push, merge, release, deploy, route/model changes, or live-trading behavior."
        )
        return {
            "status": AGENT_OS_READINESS_CHECK_WARNING,
            "promotion_status": "missing",
            "selected_scenarios_status": "missing",
            "selected_scenario_passed_only": False,
            "promotion_scope": "missing",
            "profile": "",
            "score": 0,
            "scenario_count": 0,
            "passed_count": 0,
            "pass_rate": "0/0",
            "effective_pass_rate": "0/0",
            "runner_environment_issues": 0,
            "runner_environment_recovery": "",
            "source_stability": "missing",
            "source_changes_during_run": 0,
            "scorecard_freshness": "missing",
            "source_changes_after_scorecard": 0,
            "source_change_preview_after_scorecard": "none",
            "source_churn_diagnostics_path": AGENT_SOURCE_CHURN_DIAGNOSTICS_REL_PATH,
            "source_churn_diagnostics_command": AGENT_SOURCE_CHURN_DIAGNOSTICS_COMMAND,
            "source_churn_diagnostics": source_churn_diagnostics,
            "generated_utc": "",
            "path": AGENT_CODING_BENCHMARK_SCORECARD_REL_PATH,
            "frontier_evidence_gaps": [
                _frontier_evidence_gap(
                    gate="coding_scorecard",
                    label="coding scorecard",
                    required="promotion-ready coding scorecard",
                    actual="missing",
                    path=AGENT_CODING_BENCHMARK_SCORECARD_REL_PATH,
                    next_action="Generate the coding benchmark scorecard before judging frontier readiness.",
                )
            ],
            "frontier_evidence_gap_count": 1,
            "frontier_evidence_gap_labels": ["coding scorecard"],
            "frontier_evidence_next_action": (
                "Generate the coding benchmark scorecard before judging frontier readiness."
            ),
            "frontier_evidence_handoff_label": "Copy frontier proof packet",
            "frontier_evidence_handoff_copy": "\n".join(missing_handoff_lines),
            "frontier_model_evidence_intake": frontier_model_evidence_intake,
            "frontier_evidence_intake": frontier_model_evidence_intake,
            "frontier_evidence_preflight": frontier_evidence_preflight,
            "frontier_preflight_recovery_routes": frontier_evidence_preflight["recovery_routes"],
            "frontier_preflight_recovery_route_count": frontier_evidence_preflight[
                "recovery_route_count"
            ],
            "hosted_pr_repair_candidates": hosted_pr_repair_candidates,
            "hosted_pr_repair_candidate_report_count": hosted_pr_repair_candidates[
                "candidate_count"
            ],
            "detail": "Coding benchmark scorecard is missing.",
        }

    score = _scorecard_int(metadata, "overall score")
    scenario_count = _scorecard_int(metadata, "scenarios")
    pass_rate = _scorecard_text(metadata, "pass rate")
    runner_environment_issues = _scorecard_int(metadata, "runner/environment issues")
    runner_environment_recovery = _scorecard_text(
        metadata,
        "runner/environment recovery",
    )
    promotion_status = _scorecard_text(metadata, "status", "missing").lower()
    selected_scenarios_status = _scorecard_text(
        metadata,
        "selected scenarios status",
        promotion_status,
    ).lower()
    profile = _scorecard_text(metadata, "profile")
    selected_scenarios_passed_while_failed = (
        selected_scenarios_status == AGENT_OS_READINESS_CHECK_PASSED
        and promotion_status != AGENT_OS_READINESS_CHECK_PASSED
    )
    passed_count = 0
    if "/" in pass_rate:
        try:
            passed_count = int(pass_rate.split("/", 1)[0].strip())
        except ValueError:
            passed_count = 0
    source_stability = _scorecard_text(metadata, "source stability", "unknown")
    source_changes = _scorecard_int(metadata, "source changes during run")
    source_preview = _scorecard_text(metadata, "source change preview", "none")
    source_freshness = _scorecard_source_freshness(runtime_path, metadata)
    primary_missing_capabilities = _scorecard_missing_capabilities(metadata)
    repaired_failed_rows = _coding_benchmark_repaired_failed_rows(
        runtime_path,
        metadata,
        primary_missing_capabilities,
    )
    repaired_failed_rows_count = len(repaired_failed_rows["covered_ids"])
    effective_missing_raw = repaired_failed_rows.get("missing_capabilities_after_repair")
    missing_capabilities = (
        list(effective_missing_raw)
        if isinstance(effective_missing_raw, list)
        else list(primary_missing_capabilities)
    )
    covered_missing_capabilities = list(
        repaired_failed_rows.get("covered_missing_capabilities") or []
    )
    problems: list[str] = []
    if promotion_status != AGENT_OS_READINESS_CHECK_PASSED:
        problems.append("scorecard status is not passed")
    if score < AGENT_CODING_BENCHMARK_TARGET_SCORE:
        problems.append(f"score is below {AGENT_CODING_BENCHMARK_TARGET_SCORE}")
    if scenario_count < AGENT_CODING_BENCHMARK_MIN_SCENARIOS:
        problems.append(f"scenario count is below {AGENT_CODING_BENCHMARK_MIN_SCENARIOS}")
    if passed_count != scenario_count and not repaired_failed_rows["covers_all_failed_rows"]:
        problems.append("not all scenarios passed")
    elif passed_count != scenario_count:
        problems.append(
            f"stale failed scenarios repaired in targeted rerun: {repaired_failed_rows_count}"
        )
    if runner_environment_issues:
        problems.append(
            f"{runner_environment_issues} runner/environment issue(s) require rerun"
            + (
                f": {runner_environment_recovery}"
                if runner_environment_recovery
                else ""
            )
        )
    if source_stability == "unknown":
        problems.append("source stability evidence is missing")
    elif source_stability != "stable" or source_changes:
        problems.append(
            "source/test files changed during benchmark run"
            + (f": {source_preview}" if source_preview and source_preview != "none" else "")
        )
    freshness_status = str(source_freshness.get("status") or "")
    if freshness_status == "missing_generated_utc":
        problems.append("scorecard generated UTC is missing")
    elif freshness_status == "invalid_generated_utc":
        problems.append(
            f"scorecard generated UTC is invalid: {source_freshness.get('generated_utc') or 'missing'}"
        )
    elif freshness_status == "stale":
        preview = str(source_freshness.get("source_change_preview_after_scorecard") or "none")
        problems.append(
            "source/test files changed after scorecard generation"
            + (f": {preview}" if preview and preview != "none" else "")
        )
    if missing_capabilities:
        problems.append("missing required capability coverage: " + ", ".join(missing_capabilities[:5]))
    source_evidence_unstable = (
        source_stability != "stable"
        or bool(source_changes)
        or freshness_status == "stale"
    )
    all_scenarios_effectively_passed = (
        passed_count == scenario_count
        or repaired_failed_rows["covers_all_failed_rows"]
    )
    unstable_full_evidence = (
        selected_scenarios_passed_while_failed
        and profile == "core"
        and score >= AGENT_CODING_BENCHMARK_TARGET_SCORE
        and scenario_count >= AGENT_CODING_BENCHMARK_MIN_SCENARIOS
        and all_scenarios_effectively_passed
        and not runner_environment_issues
        and not missing_capabilities
        and source_evidence_unstable
    )
    selected_scenario_passed_only = (
        selected_scenarios_passed_while_failed
        and not unstable_full_evidence
    )
    if selected_scenario_passed_only:
        problems.append(
            "selected scenarios passed only; full promotion status is "
            f"{promotion_status or 'missing'}"
        )

    model_shadow, shadow_problems = _dependent_scorecard_problem(
        runtime_path,
        AGENT_MODEL_SHADOW_EVIDENCE_SCORECARD_REL_PATH,
        min_checks=AGENT_MODEL_SHADOW_EVIDENCE_MIN_CHECKS,
        required_evidence_mode=AGENT_MODEL_SHADOW_REQUIRED_EVIDENCE_MODE,
    )
    hosted_pr_repair, hosted_problems = _dependent_scorecard_problem(
        runtime_path,
        AGENT_HOSTED_PR_REPAIR_SCORECARD_REL_PATH,
        min_checks=AGENT_HOSTED_PR_REPAIR_MIN_CHECKS,
        required_evidence_mode=AGENT_HOSTED_PR_REPAIR_REQUIRED_EVIDENCE_MODE,
        required_metadata={"promotion eligible": "true"},
    )
    model_tournament, tournament_problems = _dependent_scorecard_problem(
        runtime_path,
        AGENT_MODEL_CANDIDATE_TOURNAMENT_SCORECARD_REL_PATH,
        min_cases=AGENT_MODEL_CANDIDATE_TOURNAMENT_MIN_CASES,
        required_evidence_mode=AGENT_MODEL_CANDIDATE_TOURNAMENT_REQUIRED_EVIDENCE_MODE,
    )
    for rel_path in (
        AGENT_SYNTHETIC_REPO_REPAIR_SCORECARD_REL_PATH,
        AGENT_MODEL_PROMOTION_SCORECARD_REL_PATH,
    ):
        _summary, dependent_problems = _dependent_scorecard_problem(runtime_path, rel_path)
        problems.extend(dependent_problems)
    problems.extend(shadow_problems)
    problems.extend(tournament_problems)
    problems.extend(hosted_problems)
    frontier_evidence_gaps = _frontier_evidence_gap_summary(
        source_stability=source_stability,
        source_changes=source_changes,
        source_freshness=source_freshness,
        source_churn_diagnostics=source_churn_diagnostics,
        model_shadow=model_shadow,
        model_tournament=model_tournament,
        hosted_pr_repair=hosted_pr_repair,
        frontier_model_evidence_intake=frontier_model_evidence_intake,
        local_model_candidate_run=frontier_model_evidence_intake.get(
            "local_model_candidate_run"
        )
        or {},
    )
    frontier_evidence_gap_labels = [
        str(gap.get("label") or "")
        for gap in frontier_evidence_gaps
        if str(gap.get("label") or "").strip()
    ]
    frontier_evidence_next_action = (
        str(frontier_evidence_gaps[0].get("next_action") or "")
        if frontier_evidence_gaps
        else ""
    )

    status = AGENT_OS_READINESS_CHECK_PASSED if not problems else AGENT_OS_READINESS_CHECK_WARNING
    effective_pass_rate = (
        f"{scenario_count}/{scenario_count}"
        if repaired_failed_rows["covers_all_failed_rows"]
        else pass_rate
    )
    pass_rate_detail = (
        effective_pass_rate
        if effective_pass_rate == pass_rate
        else f"{effective_pass_rate} (raw {pass_rate})"
    )
    frontier_evidence_handoff_copy = _frontier_evidence_handoff_copy(
        score=score,
        scenario_count=scenario_count,
        pass_rate=pass_rate,
        effective_pass_rate=effective_pass_rate,
        profile=profile,
        promotion_status=promotion_status,
        selected_scenarios_status=selected_scenarios_status,
        generated_utc=str(source_freshness.get("generated_utc") or ""),
        gaps=frontier_evidence_gaps,
        preflight=frontier_evidence_preflight,
        hosted_pr_candidates=hosted_pr_repair_candidates,
    )
    if status == AGENT_OS_READINESS_CHECK_PASSED:
        detail = (
            f"Coding benchmark scorecard is {score}/"
            f"{AGENT_CODING_BENCHMARK_TARGET_SCORE} across {scenario_count} scenario(s); "
            f"pass rate {pass_rate_detail}; required capability coverage is present."
        )
    else:
        coverage_suffix = (
            " Targeted repair also covered missing capability coverage: "
            + ", ".join(covered_missing_capabilities[:5])
            + "."
            if covered_missing_capabilities and not missing_capabilities
            else ""
        )
        detail = (
            f"Coding benchmark is not promotion-ready: {score}/"
            f"{AGENT_CODING_BENCHMARK_TARGET_SCORE} across {scenario_count} scenario(s), "
            f"pass rate {pass_rate_detail}. Scorecard contract: {'; '.join(problems)}."
            f"{coverage_suffix}"
        )
    return {
        "status": status,
        "promotion_status": promotion_status,
        "selected_scenarios_status": selected_scenarios_status,
        "selected_scenario_passed_only": selected_scenario_passed_only,
        "promotion_scope": (
            "full"
            if status == AGENT_OS_READINESS_CHECK_PASSED
            else "unstable_full_evidence"
            if unstable_full_evidence
            else "selected_smoke_only"
            if selected_scenario_passed_only
            else "blocked"
        ),
        "profile": profile,
        "score": score,
        "scenario_count": scenario_count,
        "passed_count": passed_count,
        "pass_rate": pass_rate,
        "effective_pass_rate": effective_pass_rate,
        "repaired_failed_rows": repaired_failed_rows,
        "runner_environment_issues": runner_environment_issues,
        "runner_environment_recovery": runner_environment_recovery,
        "source_stability": source_stability,
        "source_changes_during_run": source_changes,
        "scorecard_freshness": freshness_status,
        "source_changes_after_scorecard": source_freshness["source_changes_after_scorecard"],
        "source_change_preview_after_scorecard": source_freshness[
            "source_change_preview_after_scorecard"
        ],
        "source_churn_diagnostics_path": AGENT_SOURCE_CHURN_DIAGNOSTICS_REL_PATH,
        "source_churn_diagnostics_command": AGENT_SOURCE_CHURN_DIAGNOSTICS_COMMAND,
        "source_churn_diagnostics": source_churn_diagnostics,
        "generated_utc": source_freshness["generated_utc"],
        "missing_capabilities": missing_capabilities,
        "primary_missing_capabilities": primary_missing_capabilities,
        "covered_missing_capabilities": covered_missing_capabilities,
        "model_shadow": model_shadow,
        "model_tournament": model_tournament,
        "hosted_pr_repair": hosted_pr_repair,
        "frontier_evidence_gaps": frontier_evidence_gaps,
        "frontier_evidence_gap_count": len(frontier_evidence_gaps),
        "frontier_evidence_gap_labels": frontier_evidence_gap_labels,
        "frontier_evidence_next_action": frontier_evidence_next_action,
        "frontier_evidence_handoff_label": (
            "Copy frontier proof packet" if frontier_evidence_handoff_copy else ""
        ),
        "frontier_evidence_handoff_copy": frontier_evidence_handoff_copy,
        "frontier_model_evidence_intake": frontier_model_evidence_intake,
        "frontier_evidence_intake": frontier_model_evidence_intake,
        "frontier_evidence_preflight": frontier_evidence_preflight,
        "frontier_preflight_recovery_routes": frontier_evidence_preflight["recovery_routes"],
        "frontier_preflight_recovery_route_count": frontier_evidence_preflight[
            "recovery_route_count"
        ],
        "hosted_pr_repair_candidates": hosted_pr_repair_candidates,
        "hosted_pr_repair_candidate_report_count": hosted_pr_repair_candidates[
            "candidate_count"
        ],
        "path": AGENT_CODING_BENCHMARK_SCORECARD_REL_PATH,
        "detail": detail,
    }

_MODEL_PREFERENCE = (
    "chili-coder:current",
    "qwen2.5-coder:7b",
    "qwen2.5-coder",
    "qwen3-coder",
    "qwen3:4b",
    "phi4-mini:latest",
    "llama3:latest",
    "llama3.2:1b",
)

_PLAN_TIMEOUT_SEC = float(os.environ.get("CHILI_PROJECT_AUTOPILOT_PLAN_TIMEOUT_SEC") or "90")
_PLAN_NUM_PREDICT = int(os.environ.get("CHILI_PROJECT_AUTOPILOT_PLAN_NUM_PREDICT") or "120")
_PLAN_NUM_CTX = int(os.environ.get("CHILI_PROJECT_AUTOPILOT_PLAN_NUM_CTX") or "2048")
_PLAN_PROMPT_CHAR_LIMIT = int(os.environ.get("CHILI_PROJECT_AUTOPILOT_PLAN_PROMPT_CHARS") or "9000")
_EDIT_TIMEOUT_SEC = float(os.environ.get("CHILI_PROJECT_AUTOPILOT_EDIT_TIMEOUT_SEC") or "150")
_EDIT_NUM_PREDICT = int(os.environ.get("CHILI_PROJECT_AUTOPILOT_EDIT_NUM_PREDICT") or "350")
_EDIT_NUM_CTX = int(os.environ.get("CHILI_PROJECT_AUTOPILOT_EDIT_NUM_CTX") or "4096")
_EDIT_MAX_FILE_LINES = int(os.environ.get("CHILI_PROJECT_AUTOPILOT_EDIT_MAX_FILE_LINES") or "260")
_OLLAMA_KEEP_ALIVE = os.environ.get("CHILI_PROJECT_AUTOPILOT_OLLAMA_KEEP_ALIVE") or "15m"
_MODEL_COOLDOWN_SEC = int(os.environ.get("CHILI_PROJECT_AUTOPILOT_MODEL_COOLDOWN_SEC") or "900")

_STAGE_ORDER = (
    "classify",
    "repo_scan",
    "plan",
    "assign_roles",
    "implement",
    "integrate",
    "validate",
    "repair",
    "merge",
    "learn",
)


class AutonomyBlocked(RuntimeError):
    """Expected stop condition that leaves the branch/worktree for review."""


class AutonomyCancelled(RuntimeError):
    """Raised when the operator cancels an active run."""


def _utcnow() -> datetime:
    return datetime.utcnow()


_PROCESS_STARTED_AT = _utcnow()


def _json_text(value: Any) -> str:
    return json.dumps(value, default=str)


def _json_load(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except Exception:
        return fallback


def _clip(text: str | None, limit: int = 6000) -> str:
    return truncate_text(text or "", max_bytes=limit)[0]


_MESSAGE_ATTACHMENT_LIMIT = 10
_IMAGE_ATTACHMENT_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})
_VIDEO_EVIDENCE_EXTS = frozenset({".mp4", ".webm", ".mov", ".mkv", ".avi"})
_VISUAL_URL_SCHEMES = frozenset({"http", "https"})
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
_RAW_MODEL_ERROR_MARKERS = (
    "http://",
    "https://",
    "urlerror",
    "timeouterror",
    "connection refused",
    "errno",
    "traceback",
    "ollama:",
)
_SCHEDULED_QUALITY_KEYS = (
    "scheduled_quality",
    "scheduled_quality_pressure",
    "report_quality",
)
_QUALITY_BAR_KEYS = (
    "quality_bar",
    "delivery_quality_bar",
    "agent_quality_bar",
)
_SCHEDULED_QUALITY_ARTIFACT_MARKERS = (
    "scheduled_quality",
    "report_quality",
    "goal_receipt",
    "goal_proof",
    "pr_receipt",
)
_QUALITY_BAR_ARTIFACT_MARKERS = (
    "quality_bar",
    "delivery_blocker",
    "pr_blocker",
    "publication_receipt",
    "pr_publication_receipt",
    "current_head_pr_publication_receipt",
    "release_trust",
    "agent_os_readiness",
    "goal_pressure",
    "recovery_brake",
    "coordination_queue",
)
_PR_PUBLICATION_RECEIPT_SCHEMA_VERSION = "chili.execution-pr-publication-receipt.v1"
_PR_PUBLICATION_REQUIRED_EVIDENCE = (
    "exact_current_head_sha",
    "current_head_check_receipt",
    "clean_merge_state",
    "pm_operator_publish_disposition",
    "selected_tests_or_ci_evidence",
)
_PR_PUBLICATION_FORBIDDEN_ACTIONS = (
    "publish_ready_claim",
    "ready_transition",
    "push_or_pr_creation",
    "pr_mutation",
    "merge",
    "release",
    "runtime_refresh",
    "live_behavior_trust",
)
_PR_PUBLICATION_DEFAULT_ALLOWED_DECISIONS = (
    "Keep blocked with owner, next check path, and blocker evidence.",
    "Close or recreate only after explicit PM/operator acceptance.",
    "Clean owner-worktree rebuild with branch, worktree, and current-head proof.",
    "Run one named owner-repair path with focused check evidence.",
)
_PR_HEALTH_PACKET_KEYS = (
    "agent_pr_blocker_health",
    "pr_blocker_health",
    "pr_health",
    "pr_blockers",
    "release_trust_summary",
)
_PR_HEALTH_ITEM_KEYS = (
    "items",
    "prs",
    "pull_requests",
    "pr_items",
    "pr_health_items",
    "pr_blocker_items",
    "blocked_items",
)
_MODEL_COOLDOWN_ERROR_MARKERS = frozenset({"timed out", "timeouterror", "timeout"})
_MODEL_COOLDOWNS: dict[str, dict[str, Any]] = {}


def _is_absolute_local_source_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    if normalized.startswith("//"):
        return False
    return normalized.startswith("/") or bool(_WINDOWS_ABSOLUTE_PATH_RE.match(path))


def _has_parent_source_path_segment(path: str) -> bool:
    parts = [part for part in path.replace("\\", "/").split("/") if part and part != "."]
    return ".." in parts


def _sanitize_absolute_local_source_path(
    raw_path: str | None,
    *,
    unsafe_reason: str,
    too_long_reason: str,
) -> tuple[str | None, str | None]:
    raw = (raw_path or "").strip().strip('"')
    if not raw:
        return None, None
    if len(raw) > ATTACHMENT_SOURCE_LIMIT:
        return None, too_long_reason
    if _CONTROL_CHAR_RE.search(raw):
        return None, unsafe_reason
    if not _WINDOWS_ABSOLUTE_PATH_RE.match(raw) and _URL_SCHEME_RE.match(raw):
        return None, unsafe_reason
    if not _is_absolute_local_source_path(raw) or _has_parent_source_path_segment(raw):
        return None, unsafe_reason
    return raw, None


def _sanitize_http_source_url(
    raw_url: str | None,
    *,
    unsafe_reason: str,
    too_long_reason: str,
) -> tuple[str | None, str | None]:
    raw = (raw_url or "").strip()
    if not raw:
        return None, None
    if len(raw) > ATTACHMENT_SOURCE_LIMIT:
        return None, too_long_reason
    if _CONTROL_CHAR_RE.search(raw):
        return None, unsafe_reason
    parsed = urlparse(raw)
    if parsed.scheme.lower() not in _VISUAL_URL_SCHEMES or not parsed.netloc:
        return None, unsafe_reason
    return raw, None


def _normalise_message_attachments(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw[:_MESSAGE_ATTACHMENT_LIMIT]:
        if not isinstance(item, dict):
            continue
        path, _ = _sanitize_absolute_local_source_path(
            str(item.get("path") or ""),
            unsafe_reason=ATTACHMENT_UNSAFE_PATH_REASON,
            too_long_reason=ATTACHMENT_PATH_TOO_LONG_REASON,
        )
        url, _ = _sanitize_http_source_url(
            str(item.get("url") or ""),
            unsafe_reason=ATTACHMENT_UNSAFE_URL_REASON,
            too_long_reason=ATTACHMENT_URL_TOO_LONG_REASON,
        )
        if not path and not url:
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            name = os.path.basename(path) if path else os.path.basename(urlparse(url or "").path)
        name = name.split("?", 1)[0].strip() or ATTACHMENT_DEFAULT_IMAGE_NAME
        mime_type = str(item.get("mime_type") or item.get("mime") or "").strip()
        source_ext = Path(path).suffix.lower() if path else Path(urlparse(url or "").path).suffix.lower()
        ext = Path(name).suffix.lower() or source_ext
        if ext not in _IMAGE_ATTACHMENT_EXTS and not mime_type.startswith(ATTACHMENT_IMAGE_MIME_PREFIX):
            continue
        clean: dict[str, Any] = {
            "kind": ATTACHMENT_KIND_IMAGE,
            "name": _clip(name, ATTACHMENT_NAME_LIMIT),
        }
        if path:
            clean["path"] = _clip(path, ATTACHMENT_SOURCE_LIMIT)
        if url:
            clean["url"] = _clip(url, ATTACHMENT_SOURCE_LIMIT)
        if mime_type:
            clean["mime_type"] = _clip(mime_type, 120)
        out.append(clean)
    return out


def _message_attachments_from_metadata(raw: str | None) -> list[dict[str, Any]]:
    metadata = _json_load(raw, {})
    if not isinstance(metadata, dict):
        return []
    return _normalise_message_attachments(metadata.get("attachments"))


def _attachment_context_source_label(item: dict[str, Any]) -> str:
    if item.get("path"):
        return ATTACHMENT_CONTEXT_LOCAL_SOURCE_LABEL
    if item.get("url"):
        return ATTACHMENT_CONTEXT_REMOTE_SOURCE_LABEL
    return ATTACHMENT_CONTEXT_SOURCELESS_LABEL


def _attachment_context(attachments: list[dict[str, Any]]) -> str:
    if not attachments:
        return ""
    lines = [ATTACHMENT_CONTEXT_HEADING]
    for idx, item in enumerate(attachments[:_MESSAGE_ATTACHMENT_LIMIT], start=1):
        name = str(item.get("name") or f"image {idx}")
        source_label = _attachment_context_source_label(item)
        lines.append(f"- {name} ({source_label})")
    return "\n".join(lines)


def _attachment_display_text(attachments: list[dict[str, Any]]) -> str:
    if not attachments:
        return ""
    if len(attachments) == 1:
        return f"Attached image: {attachments[0].get('name') or ATTACHMENT_KIND_IMAGE}"
    return f"Attached {len(attachments)} images."


def _record_attachment_artifacts(
    db: Session,
    run: ProjectAutonomyRun,
    attachments: list[dict[str, Any]],
    *,
    source: str,
) -> None:
    for idx, item in enumerate(attachments[:_MESSAGE_ATTACHMENT_LIMIT], start=1):
        _add_artifact(
            db,
            run.run_id,
            ATTACHMENT_ARTIFACT_TYPE_IMAGE,
            str(item.get("name") or f"image_{idx}"),
            content_json={"source": source, **item},
            commit=False,
        )


def _clean_visual_kind(kind: str | None) -> str:
    clean = (kind or VISUAL_KIND_SCREENSHOT).strip().lower()
    return clean if clean in {VISUAL_KIND_SCREENSHOT, VISUAL_KIND_VIDEO} else VISUAL_KIND_SCREENSHOT


def _visual_artifact_type(kind: str) -> str:
    return VISUAL_ARTIFACT_TYPE_VIDEO if kind == VISUAL_KIND_VIDEO else VISUAL_ARTIFACT_TYPE_SCREENSHOT


def _visual_allowed_exts(kind: str) -> frozenset[str]:
    return _VIDEO_EVIDENCE_EXTS if kind == VISUAL_KIND_VIDEO else _IMAGE_ATTACHMENT_EXTS


def _visual_extension_reason(kind: str) -> str:
    return VISUAL_VIDEO_EXT_REASON if kind == VISUAL_KIND_VIDEO else VISUAL_SCREENSHOT_EXT_REASON


def _sanitize_visual_evidence_path(kind: str, raw_path: str | None) -> tuple[str | None, str | None]:
    raw, reason = _sanitize_absolute_local_source_path(
        raw_path,
        unsafe_reason=VISUAL_UNSAFE_PATH_REASON,
        too_long_reason=VISUAL_PATH_TOO_LONG_REASON,
    )
    if reason or not raw:
        return None, reason
    if Path(raw).suffix.lower() not in _visual_allowed_exts(kind):
        return None, _visual_extension_reason(kind)
    return raw, None


def _sanitize_visual_evidence_url(kind: str, raw_url: str | None) -> tuple[str | None, str | None]:
    raw, reason = _sanitize_http_source_url(
        raw_url,
        unsafe_reason=VISUAL_UNSAFE_URL_REASON,
        too_long_reason=VISUAL_URL_TOO_LONG_REASON,
    )
    if reason or not raw:
        return None, reason
    parsed = urlparse(raw)
    ext = Path(parsed.path).suffix.lower()
    if ext and ext not in _visual_allowed_exts(kind):
        return None, _visual_extension_reason(kind)
    return raw, None


def _visual_skip_reason(kind: str, path_reason: str | None, url_reason: str | None) -> str:
    if path_reason:
        return path_reason
    if url_reason:
        return url_reason
    return VISUAL_VIDEO_UNAVAILABLE_REASON if kind == VISUAL_KIND_VIDEO else VISUAL_SCREENSHOT_UNAVAILABLE_REASON


def _safe_rel_path(path: str | None) -> str | None:
    raw = (path or "").replace("\\", "/").strip()
    raw = raw.lstrip("/")
    if not raw or raw.startswith("../") or "/../" in raw or raw == "..":
        return None
    return raw


def _looks_like_live_monitoring_prompt(prompt: str) -> bool:
    lower = (prompt or "").lower()
    monitoring_markers = (
        "monitor it",
        "watch it",
        "watch this",
        "right now",
        "live",
        "as i'm testing",
        "as im testing",
        "while i'm testing",
        "while im testing",
    )
    if not any(marker in lower for marker in monitoring_markers):
        return False
    implementation_markers = (
        "implement",
        "add ",
        "change ",
        "update ",
        "modify ",
        "create ",
        "build ",
        "refactor",
        "fix in ",
        "fix the file",
        "patch ",
        ".py",
        ".dart",
        ".js",
        ".ts",
        ".html",
        ".css",
        "app/",
        "chili_mobile/",
        "tests/",
    )
    return not any(marker in lower for marker in implementation_markers)


def _looks_like_greeting_or_chat(prompt: str) -> bool:
    lower = (prompt or "").strip().lower()
    if not lower:
        return True
    greeting = lower.strip(" .!?,")
    if greeting in {"hi", "hello", "hey", "yo", "sup", "good morning", "good afternoon", "good evening"}:
        return True
    chat_markers = (
        "brainstorm",
        "talk through",
        "think through",
        "discuss",
        "what do you think",
        "i have an idea",
        "i want your expertise",
    )
    implementation_markers = (
        "implement",
        "code ",
        "change ",
        "fix ",
        "add ",
        "update ",
        "modify ",
        "create ",
        "build ",
        "refactor",
        ".py",
        ".dart",
        ".js",
        ".ts",
        "app/",
        "chili_mobile/",
        "tests/",
    )
    return any(marker in lower for marker in chat_markers) and not any(
        marker in lower for marker in implementation_markers
    )


def _looks_like_plan_start_prompt(prompt: str) -> bool:
    lower = (prompt or "").strip().lower()
    if not lower:
        return False
    negative_markers = ("don't plan", "do not plan", "no plan", "not a plan")
    if any(marker in lower for marker in negative_markers):
        return False
    plan_markers = (
        "create a plan",
        "make a plan",
        "draft a plan",
        "start a plan",
        "start plan",
        "plan it",
        "plan this",
        "plan for it",
        "create plan",
        "start planning",
    )
    return any(marker in lower for marker in plan_markers)


def _friendly_model_issue(reason: str | None) -> str:
    lower = (reason or "").lower()
    if "broad desktop enhancement" in lower:
        return BROAD_DESKTOP_PLAN_ANALYSIS
    if "vague small request" in lower:
        return "This was broad enough for a conservative local planning path."
    if "timed out" in lower or "timeouterror" in lower or "timeout" in lower:
        return "The local planning model timed out."
    if "connection refused" in lower or "urlopen" in lower or "not reachable" in lower:
        return "The local planning model was not reachable."
    if "unusable model json" in lower or "usable" in lower:
        return "The local planning model did not return a usable plan."
    return "The local planning model was unavailable."


def _operator_safe_plan_text(text: str | None) -> str:
    clean = str(text or "").strip()
    if not clean:
        return ""
    lower = clean.lower()
    if "vague small request" in lower or any(
        marker in lower for marker in _RAW_MODEL_ERROR_MARKERS
    ):
        return _friendly_model_issue(clean)
    return _clip(clean, OPERATOR_SAFE_PLAN_TEXT_LIMIT)


def _has_any_token(text: str, tokens: Iterable[str]) -> bool:
    return any(token in text for token in tokens)


def _has_broad_desktop_plan_override(prompt_lower: str) -> bool:
    return _has_any_token(prompt_lower, DESKTOP_AUTOPILOT_PLAN_OVERRIDE_TOKENS)


def _is_autopilot_cockpit_request(prompt: str) -> bool:
    prompt_lower = (prompt or "").lower()
    mentions_autopilot = _has_any_token(prompt_lower, DESKTOP_AUTOPILOT_PROMPT_TOKENS)
    mentions_composer = _has_any_token(prompt_lower, DESKTOP_AUTOPILOT_COMPOSER_TOKENS)
    mentions_attachment = _has_any_token(prompt_lower, DESKTOP_AUTOPILOT_ATTACHMENT_TOKENS)
    return mentions_autopilot and mentions_composer and (
        mentions_attachment
        or "enter key" in prompt_lower
        or "send button" in prompt_lower
        or "message box" in prompt_lower
        or "prompt box" in prompt_lower
    )


def _autopilot_cockpit_reason(prompt: str) -> str:
    prompt_lower = (prompt or "").lower()
    if _has_any_token(prompt_lower, DESKTOP_AUTOPILOT_ATTACHMENT_TOKENS):
        return "The request asks for Autopilot prompt attachments; those composer controls live in the desktop cockpit screen."
    if "enter key" in prompt_lower or "send button" in prompt_lower or "message box" in prompt_lower:
        return "The request asks for Autopilot chat input behavior; the composer and send controls live in the desktop cockpit screen."
    return "The request targets the Autopilot chat cockpit, which is implemented by the desktop brain screen."


def _is_broad_desktop_enhancement_request(prompt: str) -> bool:
    prompt_lower = (prompt or "").lower()
    if not _is_vague_small_request(prompt):
        return False
    if _is_autopilot_cockpit_request(prompt):
        return False
    if _has_broad_desktop_plan_override(prompt_lower):
        return True
    if _has_any_token(prompt_lower, BROAD_DESKTOP_DETAIL_TOKENS):
        return False
    has_desktop_surface = _has_any_token(prompt_lower, DESKTOP_UI_PROMPT_TOKENS)
    has_autopilot_surface = _has_any_token(prompt_lower, DESKTOP_AUTOPILOT_PROMPT_TOKENS)
    return has_desktop_surface or has_autopilot_surface


def _broad_desktop_enhancement_plan(repo_path: Path | None) -> dict[str, Any] | None:
    target = next(
        (
            rel
            for rel in DESKTOP_AUTOPILOT_PLAN_FILES
            if _candidate_exists(repo_path, rel)
        ),
        None,
    )
    if target is None:
        return None
    return {
        "analysis": BROAD_DESKTOP_PLAN_ANALYSIS,
        "files": [
            {
                "path": target,
                "action": "modify",
                "description": BROAD_DESKTOP_PLAN_DESCRIPTION,
            }
        ],
        "notes": BROAD_DESKTOP_PLAN_NOTES,
    }


def _operator_safe_plan_payload(plan: dict[str, Any]) -> dict[str, Any]:
    if not plan:
        return {}
    safe = dict(plan)
    for key in ("analysis", "notes"):
        if key in safe:
            safe[key] = _operator_safe_plan_text(safe.get(key))
    return safe


def _pursuing_goal_explicit_contract(*values: Any) -> dict[str, Any]:
    for value in values:
        if not isinstance(value, Mapping) or not value:
            continue
        for key in ("pursuing_goal", "goal_snapshot", "goal_contract", "goal"):
            packet = value.get(key)
            if isinstance(packet, Mapping) and _pursuing_goal_objective(packet):
                return dict(packet)
        if _pursuing_goal_objective(value):
            return dict(value)
    return {}


def _pursuing_goal_objective(value: Mapping[str, Any]) -> str:
    return _clip(
        _first_text(
            value.get("objective"),
            value.get("goal"),
            value.get("active_goal_objective"),
            value.get("prompt"),
        ),
        900,
    )


def _pursuing_goal_status_label(status: str) -> str:
    if status in {RUN_STATUS_COMPLETED, RUN_STATUS_MERGED}:
        return "Goal complete"
    if status in {RUN_STATUS_BLOCKED, RUN_STATUS_FAILED, RUN_STATUS_CANCELLED}:
        return "Goal blocked"
    return "Pursuing goal"


def _pursuing_goal_current_step(status: str, stage: str) -> str:
    if status in {RUN_STATUS_COMPLETED, RUN_STATUS_MERGED}:
        return "Review completion evidence"
    if status in {RUN_STATUS_BLOCKED, RUN_STATUS_FAILED, RUN_STATUS_CANCELLED}:
        return "Inspect blocker and choose a safe recovery path"
    if stage == STAGE_CHAT:
        return "Clarify the objective before planning"
    if stage == STAGE_QUEUED:
        return "Wait for the local Autopilot worker"
    if stage == STAGE_CLASSIFY:
        return "Classify the request and safety scope"
    if stage == STAGE_REPO_SCAN:
        return "Gather repository context"
    if stage == STAGE_PLAN:
        return "Draft and review the architect plan"
    if stage == STAGE_ASSIGN_ROLES:
        return "Assign agent lanes and ownership"
    if stage == STAGE_IMPLEMENT:
        return "Implement in an isolated worktree"
    if stage == STAGE_INTEGRATE:
        return "Integrate agent work"
    if stage == STAGE_VALIDATE:
        return "Run validation gates"
    if stage == STAGE_MERGE:
        return "Check merge safety"
    if stage == STAGE_LEARN:
        return "Record the outcome as local learning evidence"
    return ""


def _pursuing_goal_next_action(row: ProjectAutonomyRun, status: str, stage: str) -> str:
    if status == RUN_STATUS_AWAITING_APPROVAL:
        return "Review the architect plan, then approve it or send feedback."
    if status == RUN_STATUS_AWAITING_CLARIFICATION:
        return "Answer the clarification before implementation can start."
    if status == RUN_STATUS_CHATTING:
        return "Start a plan when the objective and safety boundary are clear."
    if status in {RUN_STATUS_BLOCKED, RUN_STATUS_FAILED, RUN_STATUS_CANCELLED}:
        return _first_text(
            row.error_message,
            row.merge_message,
            "Start a recovery run only after the blocker and objective-tied evidence are understood.",
        )
    if status in {RUN_STATUS_COMPLETED, RUN_STATUS_MERGED}:
        return "Review validation and closeout evidence before counting the goal complete."
    if stage == STAGE_PLAN:
        return "Wait for the architect quality gate, then review the plan."
    if stage == STAGE_IMPLEMENT:
        return "Watch implementation evidence and keep the scope narrow."
    if stage == STAGE_VALIDATE:
        return "Review validation results and repair any failing gate."
    if stage == STAGE_MERGE:
        return "Confirm merge safety before accepting the result."
    return "Keep the run tied to the objective and next evidence gate."


def _pursuing_goal_completion_gate(status: str, stage: str) -> str:
    if status == RUN_STATUS_CHATTING:
        return "A plan must be started before repository scanning or edits count."
    if status == RUN_STATUS_AWAITING_APPROVAL:
        return "The architect quality gate must pass before implementation starts."
    if status in {RUN_STATUS_BLOCKED, RUN_STATUS_FAILED, RUN_STATUS_CANCELLED}:
        return "A rerun or recovery step must produce fresh objective-tied evidence."
    if status in {RUN_STATUS_COMPLETED, RUN_STATUS_MERGED}:
        return "Completion is trusted only when validation and closeout evidence match the objective."
    if stage == STAGE_VALIDATE:
        return "All required validation checks must pass or name the safe recovery path."
    return "Do not count complete until plan, implementation, validation, and closeout evidence match this objective."


def _pursuing_goal_progress_percent(status: str, stage: str) -> int:
    if status in {RUN_STATUS_COMPLETED, RUN_STATUS_MERGED}:
        return 100
    stage_progress = {
        STAGE_CHAT: 8,
        STAGE_QUEUED: 10,
        STAGE_CLASSIFY: 18,
        STAGE_REPO_SCAN: 24,
        STAGE_PLAN: 35,
        STAGE_ASSIGN_ROLES: 45,
        STAGE_IMPLEMENT: 58,
        STAGE_INTEGRATE: 68,
        STAGE_VALIDATE: 78,
        STAGE_MERGE: 88,
        STAGE_LEARN: 94,
    }.get(stage, 12)
    if status in {RUN_STATUS_BLOCKED, RUN_STATUS_FAILED, RUN_STATUS_CANCELLED}:
        return max(1, min(stage_progress, 95))
    if status == RUN_STATUS_AWAITING_APPROVAL:
        return 42
    if status == RUN_STATUS_AWAITING_CLARIFICATION:
        return 16
    if status == RUN_STATUS_CHATTING:
        return 8
    return stage_progress


def _pursuing_goal_handoff_copy(goal: Mapping[str, Any]) -> str:
    objective = _first_text(goal.get("objective"), "unknown objective")
    lines = [
        "Project Autopilot pursuing goal contract",
        f"Objective: {objective}",
        f"Status: {_first_text(goal.get('status_label'), 'Pursuing goal')} ({_first_int(goal.get('progress_percent'))}%)",
        f"Current step: {_first_text(goal.get('current_step'), 'unknown')}",
        f"Next action: {_first_text(goal.get('next_action'), 'unknown')}",
        f"Completion gate: {_first_text(goal.get('completion_gate'), 'objective-tied validation required')}",
        "",
        "Receipt rule: do not count progress unless evidence names this objective, the current step, and the next gate.",
        "Agent rule: every plan, report, recovery action, or closeout must repeat the objective and cite the evidence path or validation result it relies on.",
        "",
        "Permission boundary: this goal contract does not authorize source/test edits, commit, push, PR creation, merge, release, deploy, runtime restart, Docker, database/migration, broker/API, route/model changes, or live-trading behavior.",
    ]
    return "\n".join(lines)


def _pursuing_goal_payload(row: ProjectAutonomyRun, plan: Mapping[str, Any]) -> dict[str, Any]:
    learning = _json_load(row.learning_json, {})
    explicit = _pursuing_goal_explicit_contract(learning, plan)
    status = str(row.status or "").strip().lower()
    stage = str(row.current_stage or "").strip().lower()
    objective = _first_text(
        _pursuing_goal_objective(explicit),
        row.prompt,
        row.chat_title,
    )
    if not objective:
        return {}
    progress = _first_int(
        explicit.get("progress_percent"),
        explicit.get("progress"),
        explicit.get("completion_percent"),
        explicit.get("percent_complete"),
        _pursuing_goal_progress_percent(status, stage),
    )
    payload: dict[str, Any] = {
        "schema": "chili.project-autopilot.pursuing-goal.v1",
        "source": "explicit_goal_contract" if explicit else "backend_run_state",
        "objective": _clip(objective, 900),
        "status": status,
        "status_label": _first_text(
            explicit.get("status_label"),
            _pursuing_goal_status_label(status),
        ),
        "current_step": _first_text(
            explicit.get("current_step"),
            explicit.get("currentStep"),
            explicit.get("step"),
            _pursuing_goal_current_step(status, stage),
        ),
        "next_action": _first_text(
            explicit.get("next_action"),
            explicit.get("nextAction"),
            explicit.get("operator_next_action"),
            _pursuing_goal_next_action(row, status, stage),
        ),
        "completion_gate": _first_text(
            explicit.get("completion_gate"),
            explicit.get("completionGate"),
            explicit.get("done_when"),
            explicit.get("acceptance_gate"),
            _pursuing_goal_completion_gate(status, stage),
        ),
        "progress_percent": max(0, min(progress, 100)),
        "progress_authority": _first_text(
            explicit.get("progress_authority"),
            "Recorded run stage, plan, architect review, validation, operator action, and artifact evidence are authoritative.",
        ),
        "receipt_sections": _string_sequence(explicit.get("receipt_sections"))
        or ["Objective", "Current evidence", "Checks", "Next gate"],
        "receipt_trust_rule": _first_text(
            explicit.get("receipt_trust_rule"),
            "Do not count progress unless the run evidence names this objective and the next gate.",
        ),
        "receipt_safety_boundary": _first_text(
            explicit.get("receipt_safety_boundary"),
            "No merge, release, broker, or live action counts unless an explicit gate records it.",
        ),
        "success_criteria": _string_sequence(
            explicit.get("success_criteria")
            or explicit.get("acceptance_criteria")
            or explicit.get("done_when")
        ),
        "evidence_required": _string_sequence(explicit.get("evidence_required"))
        or [
            "objective_repeated",
            "current_step_named",
            "next_gate_named",
            "evidence_path_or_validation_result",
        ],
        "forbidden_completion_claims": _string_sequence(
            explicit.get("forbidden_completion_claims")
        )
        or [
            "generic_progress_without_objective",
            "complete_without_validation_or_closeout",
            "ready_or_merge_claim_without_current_gate_evidence",
        ],
        "context_handoff_label": _first_text(
            explicit.get("context_handoff_label"),
            explicit.get("handoff_label"),
            "Copy goal contract",
        ),
    }
    payload["context_handoff_copy"] = _first_text(
        explicit.get("context_handoff_copy"),
        explicit.get("handoff"),
        _pursuing_goal_handoff_copy(payload),
    )
    payload["agent_prompt_contract"] = "\n".join(
        [
            f"Active objective: {payload['objective']}",
            f"Current step: {payload['current_step']}",
            f"Next gate: {payload['completion_gate']}",
            "Every agent report must name objective-tied evidence before claiming progress.",
            "This contract grants no source, git, PR, runtime, database, broker, release, or live-trading authority.",
        ]
    )
    return payload


def _run_payload(row: ProjectAutonomyRun) -> dict[str, Any]:
    plan = _json_load(row.plan_json, {})
    return {
        "id": row.id,
        "run_id": row.run_id,
        "project_run_id": row.project_run_id,
        "user_id": row.user_id,
        "repo_id": row.repo_id,
        "prompt": row.prompt,
        "status": row.status,
        "current_stage": row.current_stage,
        "autonomy_level": row.autonomy_level,
        "execution_mode": row.execution_mode,
        "plan_status": row.plan_status,
        "chat_title": row.chat_title,
        "model_policy": row.model_policy,
        "target_branch": row.target_branch,
        "base_branch": row.base_branch,
        "base_sha": row.base_sha,
        "integration_branch": row.integration_branch,
        "worktree_path": row.worktree_path,
        "merge_status": row.merge_status,
        "merge_message": row.merge_message,
        "plan": _operator_safe_plan_payload(plan),
        "pursuing_goal": _pursuing_goal_payload(row, plan if isinstance(plan, Mapping) else {}),
        "architect_review": {},
        "agents": _json_load(row.agents_json, []),
        "files": _json_load(row.files_json, []),
        "commands": _json_load(row.commands_json, []),
        "validation": _json_load(row.validation_json, []),
        "learning": _json_load(row.learning_json, {}),
        "error_message": row.error_message,
        "cancel_requested": bool(row.cancel_requested),
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _step_payload(row: ProjectAutonomyStep) -> dict[str, Any]:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "step_index": row.step_index,
        "stage": row.stage,
        "agent_name": row.agent_name,
        "status": row.status,
        "title": row.title,
        "detail": _json_load(row.detail_json, {}),
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _artifact_payload(row: ProjectAutonomyArtifact) -> dict[str, Any]:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "artifact_type": row.artifact_type,
        "name": row.name,
        "content": row.content,
        "content_json": _json_load(row.content_json, None),
        "byte_length": row.byte_length,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _architect_review_payload(row: ProjectAutonomyArchitectReview | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {
        "id": row.id,
        "run_id": row.run_id,
        "attempt_index": row.attempt_index,
        "status": row.status,
        "score": row.score,
        "confidence": row.confidence,
        "dimensions": _json_load(row.dimensions_json, {}),
        "alternatives": _json_load(row.alternatives_json, []),
        "critique": _json_load(row.critique_json, {}),
        "selected_files": _json_load(row.selected_files_json, []),
        "blocking_reason": row.blocking_reason,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _message_payload(row: ProjectAutonomyMessage) -> dict[str, Any]:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "role": row.role,
        "message_type": row.message_type,
        "content": row.content,
        "metadata": _json_load(row.metadata_json, {}),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _scheduled_quality_from_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    for key in _SCHEDULED_QUALITY_KEYS:
        packet = value.get(key)
        if isinstance(packet, Mapping) and packet:
            return dict(packet)
    quality_bar = _quality_bar_from_mapping(value)
    for key in _SCHEDULED_QUALITY_KEYS:
        packet = quality_bar.get(key)
        if isinstance(packet, Mapping) and packet:
            return dict(packet)
    return {}


def _quality_bar_with_pr_publication_preflight(packet: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(packet)
    groups = normalized.get("delivery_blocker_groups")
    if not isinstance(groups, list) or not groups:
        return normalized

    updated_groups: list[Any] = []
    changed = False
    for group in groups:
        if not isinstance(group, Mapping):
            updated_groups.append(group)
            continue
        group_payload = dict(group)
        if str(group_payload.get("key") or "").strip() == "pr_blocker_train":
            enriched = _pr_publication_preflight_group(group_payload, normalized)
            changed = changed or enriched != group_payload
            group_payload = enriched
        updated_groups.append(group_payload)
    if changed:
        normalized["delivery_blocker_groups"] = updated_groups
    return normalized


def _pr_publication_preflight_group(
    group: Mapping[str, Any],
    packet: Mapping[str, Any],
) -> dict[str, Any]:
    enriched = dict(group)
    receipt = _first_mapping(
        enriched.get("publication_receipt"),
        enriched.get("pr_publication_receipt"),
        enriched.get("current_head_pr_publication_receipt"),
        packet.get("publication_receipt"),
        packet.get("pr_publication_receipt"),
        packet.get("current_head_pr_publication_receipt"),
    )
    if receipt and _pr_publication_receipt_ready(receipt):
        return enriched

    missing_evidence = _dedupe_texts(
        [
            *_string_sequence(receipt.get("missing_evidence") if receipt else None),
            *_string_sequence(enriched.get("required_evidence")),
            *_PR_PUBLICATION_REQUIRED_EVIDENCE,
        ]
    )
    required_evidence = _dedupe_texts(
        [
            *_string_sequence(enriched.get("required_evidence")),
            *_string_sequence(receipt.get("required_evidence") if receipt else None),
            *_PR_PUBLICATION_REQUIRED_EVIDENCE,
        ]
    )
    recovery_decision = _pr_publication_recovery_decision(
        enriched,
        missing_evidence=missing_evidence,
    )
    handoff_copy = _first_text(
        enriched.get("pr_publish_packet_copy"),
        enriched.get("report_pr_publish_packet_copy"),
    )
    if not handoff_copy:
        handoff_copy = _pr_publication_preflight_handoff_copy(
            enriched,
            missing_evidence=missing_evidence,
            required_evidence=required_evidence,
            recovery_decision=recovery_decision,
        )
    blockers = _pr_publication_blockers(enriched, missing_evidence=missing_evidence)

    receipt_payload = dict(receipt) if receipt else {}
    receipt_payload.setdefault("schema", _PR_PUBLICATION_RECEIPT_SCHEMA_VERSION)
    receipt_payload.setdefault("status", "warning")
    receipt_payload.setdefault("publication_proof_ready", False)
    receipt_payload.setdefault("missing_evidence", missing_evidence)
    receipt_payload.setdefault("required_evidence", required_evidence)
    receipt_payload.setdefault("proof_items", _pr_publication_proof_items(enriched))
    receipt_payload.setdefault("blocked_action_label", "Blocked until PR proof")
    receipt_payload.setdefault("next_action_handoff_label", "Copy PR publication gate")
    receipt_payload.setdefault("next_action_handoff_copy", handoff_copy)
    enriched["publication_receipt"] = receipt_payload
    enriched.setdefault("pr_publish_verdict", "not_publishable")
    enriched.setdefault(
        "pr_publish_gate_state",
        "blocked_until_current_head_publication_receipt",
    )
    enriched.setdefault("pr_publish_next_gate", "current_head_check_receipt")
    enriched.setdefault("pr_publish_blockers", blockers)
    enriched.setdefault("pr_publish_required_evidence", required_evidence)
    enriched.setdefault(
        "pr_publish_forbidden_actions",
        list(_PR_PUBLICATION_FORBIDDEN_ACTIONS),
    )
    enriched.setdefault(
        "pr_publish_first_action_owner",
        recovery_decision["owner"],
    )
    enriched.setdefault(
        "pr_publish_first_action_label",
        recovery_decision["first_action"],
    )
    enriched.setdefault(
        "pr_publish_first_action_proof",
        recovery_decision["proof"],
    )
    enriched.setdefault("pr_recovery_decision", recovery_decision["decision"])
    enriched.setdefault("pr_recovery_decision_label", recovery_decision["label"])
    enriched.setdefault("pr_recovery_owner", recovery_decision["owner"])
    enriched.setdefault("pr_recovery_safe_next_step", recovery_decision["first_action"])
    enriched.setdefault("pr_recovery_required_proof", recovery_decision["proof"])
    enriched.setdefault("pr_publish_action_plan", [recovery_decision])
    enriched.setdefault(
        "pr_publish_operator_summary",
        "Do not trust green, publish-ready, ready-transition, merge-ready, or repair-complete claims until current-head PR proof exists.",
    )
    enriched.setdefault("pr_publish_packet_label", "Copy PR publication gate")
    enriched.setdefault("pr_publish_packet_copy", handoff_copy)
    enriched.setdefault(
        "blocked_action",
        "Do not mutate PR state, push, create PRs, merge, or start broad source repair.",
    )
    if not _string_sequence(enriched.get("allowed_decisions")):
        enriched["allowed_decisions"] = list(_PR_PUBLICATION_DEFAULT_ALLOWED_DECISIONS)
    return enriched


def _pr_publication_recovery_decision(
    group: Mapping[str, Any],
    *,
    missing_evidence: Sequence[str],
) -> dict[str, str]:
    pr = _first_text(group.get("top_pr"), group.get("pr_number"), group.get("pr"))
    branch = _first_text(group.get("top_branch"), group.get("branch"))
    top_ci = str(group.get("top_ci") or group.get("ci") or "").strip().lower()
    top_merge = str(group.get("top_merge") or group.get("merge_state") or "").strip().lower()
    posture = str(group.get("top_posture") or group.get("posture") or "").strip().lower()
    gate_state = str(group.get("gate_state") or "").strip().lower()
    source_text = " ".join(
        str(group.get(key) or "")
        for key in (
            "gate_state",
            "gate_label",
            "top_posture",
            "blocked_action",
            "next_action_detail",
            "required_proof",
            "owner_agent_next_action",
        )
    ).lower()
    subject = f"PR #{pr}" if pr else f"branch {branch}" if branch else "this PR lane"

    if any(
        marker in source_text
        for marker in (
            "pm_operator_disposition_required",
            "pm/operator disposition",
            "terminal captain",
            "close/recreate",
            "clean owner-worktree",
            "clean rebuild",
            "one named repair path",
        )
    ):
        return {
            "decision": "wait_for_operator_disposition",
            "label": "Wait for PM/operator PR disposition",
            "owner": "PM/operator",
            "first_action": (
                f"{subject}: choose keep blocked, close/recreate, clean rebuild, "
                "exact current-head acceptance, or one named repair path before PR movement."
            ),
            "proof": (
                "Disposition artifact must name the PR/branch, head SHA, accepted path, "
                "owner lane, current-head gates, and forbidden actions."
            ),
        }

    if top_merge and top_merge != "clean":
        return {
            "decision": "resolve_or_recreate_pr",
            "label": "Resolve merge state or close/recreate",
            "owner": _first_text(group.get("owner_agent"), group.get("owner"), "PR owner"),
            "first_action": (
                f"{subject}: keep frozen until a clean owner-worktree rebuild, "
                "close/recreate decision, or accepted current-head merge proof exists."
            ),
            "proof": (
                "Required proof: clean owner worktree, branch, head SHA, clean merge state, "
                "focused checks, and PM/operator acceptance for close/recreate or rebuild."
            ),
        }

    if "fail" in top_ci or "ci_failing" in posture:
        return {
            "decision": "repair_current_head_ci",
            "label": "Run one named owner repair path",
            "owner": _first_text(group.get("owner_agent"), group.get("owner"), "PR owner"),
            "first_action": (
                f"{subject}: repair only one named failing current-head check path in an isolated owner worktree."
            ),
            "proof": (
                "Required proof: failing check URL/name, exact head SHA, changed files, focused test output, "
                "and refreshed current-head check receipt."
            ),
        }

    if "pending" in top_ci or "ci_pending" in posture:
        return {
            "decision": "wait_for_current_head_checks",
            "label": "Wait for current-head checks",
            "owner": _first_text(group.get("owner_agent"), group.get("owner"), "DevOps/PR owner"),
            "first_action": (
                f"{subject}: wait for or refresh exact-head check receipts before ready, "
                "repair-complete, publish, or merge claims."
            ),
            "proof": (
                "Receipt must name PR, branch, head SHA, completed check result, source URL, "
                "and timestamp."
            ),
        }

    if "no check" in top_ci or "ci_missing_checks" in posture:
        return {
            "decision": "attach_current_head_checks",
            "label": "Attach current-head checks",
            "owner": _first_text(group.get("owner_agent"), group.get("owner"), "DevOps/PR owner"),
            "first_action": (
                f"{subject}: attach exact-head check receipts before ready, repair-complete, publish, or merge claims."
            ),
            "proof": (
                "Receipt must name PR, branch, head SHA, successful check, source URL, and timestamp."
            ),
        }

    if not pr and (
        str(group.get("existing_pr_found") or "").strip().lower() == "false"
        or "create_draft_pr" in gate_state
    ):
        return {
            "decision": "prepare_draft_pr_packet",
            "label": "Prepare draft PR packet",
            "owner": _first_text(group.get("owner_agent"), group.get("owner"), "PR owner"),
            "first_action": (
                f"{subject}: attach branch, head SHA, clean worktree, focused checks, and operator approval before draft PR creation."
            ),
            "proof": (
                "Draft packet must prove no existing PR owns the branch and must name target base, branch, head SHA, checks, and requested reviewers."
            ),
        }

    return {
        "decision": "attach_current_head_publication_receipt",
        "label": "Attach current-head publication receipt",
        "owner": _first_text(group.get("owner_agent"), group.get("owner"), "PR owner"),
        "first_action": (
            f"{subject}: attach the current-head publication receipt before any ready, repair-complete, publish, or merge claim."
        ),
        "proof": (
            "Receipt must name PR/branch, head SHA, successful check, source URL, timestamp, and PM/operator disposition."
        ),
    }


def _pr_publication_receipt_ready(receipt: Mapping[str, Any]) -> bool:
    status = str(receipt.get("status") or receipt.get("state") or "").strip().lower()
    ready = receipt.get("publication_proof_ready", receipt.get("proof_ready"))
    if isinstance(ready, bool):
        return ready and not _string_sequence(receipt.get("missing_evidence"))
    return status == AGENT_OS_READINESS_CHECK_PASSED and not _string_sequence(
        receipt.get("missing_evidence")
    )


def _pr_publication_blockers(
    group: Mapping[str, Any],
    *,
    missing_evidence: Sequence[str],
) -> list[str]:
    blockers: list[str] = []
    top_ci = str(group.get("top_ci") or group.get("ci") or "").strip().lower()
    top_merge = str(group.get("top_merge") or group.get("merge_state") or "").strip().lower()
    top_posture = str(group.get("top_posture") or group.get("posture") or "").strip().lower()
    if "current_head_check_receipt" in missing_evidence:
        blockers.append("current_head_check_receipt_missing")
    if "no check" in top_ci or "ci_missing_checks" in top_posture:
        blockers.append("current_head_checks_missing")
    if "fail" in top_ci or "ci_failing" in top_posture:
        blockers.append("current_head_ci_failing")
    if "pending" in top_ci or "ci_pending" in top_posture:
        blockers.append("current_head_ci_pending")
    if top_merge and top_merge != "clean":
        blockers.append("merge_state_not_clean")
    if str(group.get("gate_state") or "").strip():
        blockers.append(str(group.get("gate_state")).strip())
    blockers.append("pm_operator_publish_disposition_missing")
    return _dedupe_texts([*blockers, *missing_evidence])


def _pr_publication_proof_items(group: Mapping[str, Any]) -> list[str]:
    items = []
    pr = _first_text(group.get("top_pr"), group.get("pr_number"), group.get("pr"))
    branch = _first_text(group.get("top_branch"), group.get("branch"))
    head = _first_text(group.get("head_sha"), group.get("head_ref_oid"), group.get("commit_sha"))
    merge = _first_text(group.get("top_merge"), group.get("merge_state"))
    ci = _first_text(group.get("top_ci"), group.get("ci_state"), group.get("ci"))
    if pr:
        items.append(f"PR #{pr}")
    if branch:
        items.append(f"branch {branch}")
    if head:
        items.append(f"head {head[:12]}")
    if merge:
        items.append(f"merge {merge}")
    if ci:
        items.append(f"ci {ci}")
    return items


def _pr_publication_preflight_handoff_copy(
    group: Mapping[str, Any],
    *,
    missing_evidence: Sequence[str],
    required_evidence: Sequence[str],
    recovery_decision: Mapping[str, str] | None = None,
) -> str:
    pr = _first_text(group.get("top_pr"), group.get("pr_number"), group.get("pr"), "unknown")
    branch = _first_text(group.get("top_branch"), group.get("branch"), "unknown")
    merge = _first_text(group.get("top_merge"), group.get("merge_state"), "unknown")
    ci = _first_text(group.get("top_ci"), group.get("ci_state"), group.get("ci"), "unknown")
    path = _first_text(group.get("next_action_path"), group.get("path"), "project_ws/AgentOps/OMNIAGENT_KPI_SCORECARD.md")
    allowed = _string_sequence(group.get("allowed_decisions")) or list(
        _PR_PUBLICATION_DEFAULT_ALLOWED_DECISIONS
    )
    lines = [
        "Project Autopilot PR publication decision packet",
        "Purpose: decide whether a PR can be created, pushed, marked ready, repaired, or published.",
        "Verdict: not_publishable until current-head publication proof is attached.",
        "",
        "Current PR pressure:",
        f"- PR: #{pr}",
        f"- Branch: {branch}",
        f"- Merge: {merge}",
        f"- CI/checks: {ci}",
        f"- Evidence path: {path}",
        "",
        "Recovery decision:",
        f"- Decision: {(recovery_decision or {}).get('label') or 'Attach current-head publication receipt'}",
        f"- Owner: {(recovery_decision or {}).get('owner') or 'PR owner'}",
        f"- First action: {(recovery_decision or {}).get('first_action') or 'Attach current-head publication proof before PR movement.'}",
        f"- Proof required: {(recovery_decision or {}).get('proof') or 'Receipt must name PR, branch, head SHA, check, URL, and timestamp.'}",
        "",
        "Required evidence before any PR movement:",
    ]
    lines.extend(f"- {item}" for item in required_evidence)
    if missing_evidence:
        lines.append("")
        lines.append("Currently missing:")
        lines.extend(f"- {item}" for item in missing_evidence)
    lines.append("")
    lines.append("Allowed decisions:")
    lines.extend(f"- {item}" for item in allowed)
    lines.append("")
    lines.append("Forbidden actions until the gate clears:")
    lines.extend(f"- {item}" for item in _PR_PUBLICATION_FORBIDDEN_ACTIONS)
    lines.extend(
        [
            "",
            "Permission boundary: review and evidence routing only. This packet does not authorize source/test edits, commit, push, PR creation, ready transition, PR mutation, merge, release, deploy, runtime restart, Docker, database/migration, broker/API, route/model changes, or live-trading behavior.",
        ]
    )
    return "\n".join(lines)


def _first_mapping(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, Mapping) and value:
            return dict(value)
    return {}


def _string_sequence(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, Iterable) and not isinstance(value, Mapping):
        return [
            str(item).strip()
            for item in value
            if str(item).strip()
        ]
    return []


def _dedupe_texts(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        clean = str(value).strip()
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(clean)
    return out


def _first_text(*values: Any) -> str:
    for value in values:
        clean = str(value or "").strip()
        if clean:
            return clean
    return ""


def _first_int(*values: Any) -> int:
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        clean = str(value or "").strip()
        if not clean:
            continue
        try:
            return int(clean)
        except ValueError:
            continue
    return 0


def _bool_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    clean = str(value or "").strip().lower()
    if not clean:
        return None
    if clean in {"1", "true", "yes", "y", "blocked", "warning", "failed", "failing"}:
        return True
    if clean in {"0", "false", "no", "n", "clear", "passed", "passing", "green", "none"}:
        return False
    return None


def _looks_like_pr_health_item(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    has_identity = any(
        key in value
        for key in (
            "number",
            "pr_number",
            "top_pr",
            "pr",
            "url",
            "branch",
            "pr_branch",
            "top_branch",
            "head_ref_oid",
            "head_sha",
        )
    )
    has_health = any(
        key in value
        for key in (
            "ci_state",
            "ci_summary",
            "top_ci",
            "blocker_kind",
            "top_posture",
            "merge_state",
            "top_merge",
            "blocked",
            "ready_candidate",
            "pr_publish_verdict",
        )
    )
    return has_identity and has_health


def _pr_health_packets_from_mapping(value: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    packets: list[Mapping[str, Any]] = []
    if _looks_like_pr_health_item(value) or any(
        isinstance(value.get(key), list) for key in _PR_HEALTH_ITEM_KEYS
    ):
        packets.append(value)

    for key in _PR_HEALTH_PACKET_KEYS:
        packet = value.get(key)
        if isinstance(packet, Mapping) and packet:
            packets.append(packet)

    operator_inbox = value.get("operator_inbox")
    if isinstance(operator_inbox, Mapping):
        release_trust = operator_inbox.get("release_trust_summary")
        if isinstance(release_trust, Mapping) and release_trust:
            packets.append(release_trust)
        for key in _PR_HEALTH_PACKET_KEYS:
            packet = operator_inbox.get(key)
            if isinstance(packet, Mapping) and packet:
                packets.append(packet)

    out: list[Mapping[str, Any]] = []
    seen: set[int] = set()
    for packet in packets:
        identity = id(packet)
        if identity in seen:
            continue
        seen.add(identity)
        out.append(packet)
    return out


def _pr_health_items(packet: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if _looks_like_pr_health_item(packet):
        return [packet]
    items: list[Mapping[str, Any]] = []
    for key in _PR_HEALTH_ITEM_KEYS:
        raw_items = packet.get(key)
        if not isinstance(raw_items, Iterable) or isinstance(
            raw_items,
            (str, bytes, Mapping),
        ):
            continue
        for item in raw_items:
            if _looks_like_pr_health_item(item):
                items.append(item)
    return items


def _pr_health_item_blocked(item: Mapping[str, Any]) -> bool:
    explicit = _bool_value(item.get("blocked"))
    if explicit is True:
        return True

    posture = _first_text(
        item.get("blocker_kind"),
        item.get("top_posture"),
        item.get("posture"),
        item.get("category"),
        item.get("report_blocker_category"),
    ).lower()
    if posture and posture not in {"none", "clear", "green", "ok", "ready"}:
        return True

    ci_state = _first_text(
        item.get("ci_state"),
        item.get("top_ci"),
        item.get("ci"),
    ).lower()
    normalized_ci = ci_state.replace("_", " ")
    if any(marker in normalized_ci for marker in ("fail", "pending", "no check", "missing")):
        return True

    merge = _first_text(item.get("merge_state"), item.get("top_merge")).lower()
    if merge and merge not in {"clean", "green", "passed", "passing", "ok"}:
        return True

    return False


def _pr_health_check_names(item: Mapping[str, Any], key: str) -> list[str]:
    checks = item.get(key)
    if not isinstance(checks, Iterable) or isinstance(checks, (str, bytes, Mapping)):
        return _string_sequence(checks)
    names: list[str] = []
    for check in checks:
        if isinstance(check, Mapping):
            names.append(
                _first_text(
                    check.get("name"),
                    check.get("context"),
                    check.get("workflowName"),
                    check.get("workflow_name"),
                    check.get("url"),
                )
            )
        else:
            names.append(str(check or "").strip())
    return _dedupe_texts(names)


def _pr_health_source_path(item: Mapping[str, Any], packet: Mapping[str, Any]) -> str:
    return _first_text(
        item.get("next_action_path"),
        item.get("path"),
        item.get("source_path"),
        packet.get("next_action_path"),
        packet.get("path"),
        "project_ws/SRE/OUT/_state/agent-pr-blocker-health.json",
    )


def _pr_blocker_group_from_health_item(
    item: Mapping[str, Any],
    packet: Mapping[str, Any],
    *,
    count: int,
) -> dict[str, Any]:
    pr = _first_text(
        item.get("number"),
        item.get("pr_number"),
        item.get("top_pr"),
        item.get("pr"),
    )
    branch = _first_text(
        item.get("branch"),
        item.get("pr_branch"),
        item.get("headRefName"),
        item.get("head_ref_name"),
        item.get("top_branch"),
    )
    head = _first_text(
        item.get("head_ref_oid"),
        item.get("headRefOid"),
        item.get("head_sha"),
        item.get("commit_sha"),
    )
    merge = _first_text(
        item.get("merge_state"),
        item.get("mergeStateStatus"),
        item.get("top_merge"),
    )
    ci = _first_text(
        item.get("ci_summary"),
        item.get("ci_state"),
        item.get("top_ci"),
        item.get("ci"),
    )
    ci_state = _first_text(item.get("ci_state"), item.get("top_ci"), item.get("ci"))
    posture = _first_text(
        item.get("blocker_kind"),
        item.get("top_posture"),
        item.get("posture"),
        item.get("category"),
        item.get("report_blocker_category"),
    )
    path = _pr_health_source_path(item, packet)
    failing_checks = _pr_health_check_names(item, "failing_checks")
    pending_checks = _pr_health_check_names(item, "pending_checks")

    detail_parts = []
    if pr:
        detail_parts.append(f"PR #{pr}")
    if branch:
        detail_parts.append(f"branch {branch}")
    if head:
        detail_parts.append(f"head {head[:12]}")
    if merge:
        detail_parts.append(f"merge {merge}")
    if ci:
        detail_parts.append(f"checks {ci}")
    if posture:
        detail_parts.append(f"blocker {posture}")
    if failing_checks:
        detail_parts.append("failing checks: " + ", ".join(failing_checks[:3]))
    if pending_checks:
        detail_parts.append("pending checks: " + ", ".join(pending_checks[:3]))

    group: dict[str, Any] = {
        "key": "pr_blocker_train",
        "count": count,
        "gate_label": _first_text(
            item.get("gate_label"),
            packet.get("gate_label"),
            "Blocked until PR proof",
        ),
        "gate_state": _first_text(
            item.get("gate_state"),
            item.get("pr_publish_gate_state"),
            packet.get("gate_state"),
            "agent_pr_blocker_health_current_head_gate",
        ),
        "top_pr": pr,
        "top_branch": branch,
        "head_sha": head,
        "top_merge": merge,
        "top_ci": ci,
        "ci_state": ci_state,
        "top_posture": posture,
        "next_action_path": path,
        "next_action_open_path": _first_text(
            item.get("open_path"),
            packet.get("open_path"),
        ),
        "next_action_kind": "pr_blocker_train",
        "next_action_detail": _first_text(
            item.get("next_action_detail"),
            item.get("detail"),
            ". ".join(detail_parts),
        ),
        "owner_agent": _first_text(
            item.get("owner_agent"),
            item.get("owner"),
            item.get("agent"),
            item.get("report_expedite_owner"),
            packet.get("next_action_agent"),
            "PR owner",
        ),
        "url": _first_text(item.get("url"), item.get("html_url")),
        "base": _first_text(item.get("base"), item.get("baseRefName")),
        "source_kind": _first_text(
            item.get("source_kind"),
            packet.get("source_kind"),
            "agent_pr_blocker_health",
        ),
        "pr_health_source": "agent_pr_blocker_health",
        "checked_open_pr_count": _first_int(packet.get("checked_open_pr_count")),
    }
    if failing_checks:
        group["failing_check_names"] = failing_checks
    if pending_checks:
        group["pending_check_names"] = pending_checks

    for key in (
        "publication_receipt",
        "pr_publication_receipt",
        "current_head_pr_publication_receipt",
        "pr_publish_verdict",
        "pr_publish_gate_state",
        "pr_publish_next_gate",
        "pr_publish_blockers",
        "pr_publish_required_evidence",
        "pr_publish_forbidden_actions",
        "pr_publish_packet_label",
        "pr_publish_packet_copy",
        "report_pr_publish_packet_label",
        "report_pr_publish_packet_copy",
        "allowed_decisions",
        "blocked_action",
    ):
        if key in item:
            group[key] = item[key]
    return group


def _quality_bar_from_pr_health_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    for packet in _pr_health_packets_from_mapping(value):
        items = _pr_health_items(packet)
        if not items:
            continue
        blocked_items = [item for item in items if _pr_health_item_blocked(item)]
        blocker_count = _first_int(
            packet.get("ci_blocked_count"),
            packet.get("blocker_count"),
            packet.get("blocked_count"),
            len(blocked_items),
        )
        if not blocked_items and blocker_count <= 0:
            continue
        top_item = blocked_items[0] if blocked_items else items[0]
        count = max(blocker_count, len(blocked_items), 1)
        group = _pr_blocker_group_from_health_item(top_item, packet, count=count)
        quality_bar = {
            "source": "agent_pr_blocker_health",
            "schema": "chili.agent-pr-blocker-health.quality-bar.v1",
            "generated_utc": _first_text(
                packet.get("generated_utc"),
                packet.get("created_at"),
            ),
            "repo": _first_text(packet.get("repo")),
            "checked_open_pr_count": _first_int(packet.get("checked_open_pr_count")),
            "ci_blocked_count": count,
            "delivery_blocker_groups": [group],
        }
        return _quality_bar_with_pr_publication_preflight(quality_bar)
    return {}


def _quality_bar_from_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    for key in _QUALITY_BAR_KEYS:
        packet = value.get(key)
        if isinstance(packet, Mapping) and packet:
            pr_health_packet = _quality_bar_from_pr_health_mapping(packet)
            if pr_health_packet:
                return pr_health_packet
            return _quality_bar_with_pr_publication_preflight(packet)
    blockers = value.get("delivery_blocker_groups")
    if isinstance(blockers, list) and blockers:
        return _quality_bar_with_pr_publication_preflight(value)
    pr_health_packet = _quality_bar_from_pr_health_mapping(value)
    if pr_health_packet:
        return pr_health_packet
    if _looks_like_pr_publication_receipt_mapping(value):
        return dict(value)
    return {}


def _looks_like_pr_publication_receipt_mapping(value: Mapping[str, Any]) -> bool:
    for key in (
        "publication_receipt",
        "pr_publication_receipt",
        "current_head_pr_publication_receipt",
    ):
        packet = value.get(key)
        if isinstance(packet, Mapping) and packet:
            return True
    schema = str(value.get("schema") or "").lower()
    return (
        "publication-receipt" in schema
        or "publication_receipt" in schema
        or "publication_proof_ready" in value
        or "missing_evidence" in value
        or "proof_items" in value
    )


def _artifact_scheduled_quality_payload(artifact: ProjectAutonomyArtifact) -> dict[str, Any]:
    label = f"{artifact.artifact_type or ''} {artifact.name or ''}".lower()
    if not any(marker in label for marker in _SCHEDULED_QUALITY_ARTIFACT_MARKERS):
        return {}
    content = _json_load(artifact.content_json, None)
    nested = _scheduled_quality_from_mapping(content)
    if nested:
        return nested
    if isinstance(content, Mapping) and content:
        return dict(content)
    return {}


def _artifact_quality_bar_payload(artifact: ProjectAutonomyArtifact) -> dict[str, Any]:
    content = _json_load(artifact.content_json, None)
    packet = _quality_bar_from_mapping(content)
    if packet:
        return packet
    label = f"{artifact.artifact_type or ''} {artifact.name or ''}".lower()
    if not any(marker in label for marker in _QUALITY_BAR_ARTIFACT_MARKERS):
        return {}
    if isinstance(content, Mapping) and content:
        return dict(content)
    return {}


def _scheduled_quality_payload(
    db: Session,
    row: ProjectAutonomyRun,
    *,
    artifacts: Iterable[ProjectAutonomyArtifact] | None = None,
) -> dict[str, Any]:
    learning = _json_load(row.learning_json, {})
    packet = _scheduled_quality_from_mapping(learning)
    if packet:
        return packet

    agents = _json_load(row.agents_json, [])
    agent_items = agents if isinstance(agents, list) else [agents]
    for agent in agent_items:
        packet = _scheduled_quality_from_mapping(agent)
        if packet:
            return packet

    artifact_rows = artifacts
    if artifact_rows is None:
        artifact_rows = (
            db.query(ProjectAutonomyArtifact)
            .filter(ProjectAutonomyArtifact.run_id == row.run_id)
            .order_by(ProjectAutonomyArtifact.id.desc())
            .limit(24)
            .all()
        )
    for artifact in artifact_rows:
        packet = _artifact_scheduled_quality_payload(artifact)
        if packet:
            return packet
    return {}


def _quality_bar_payload(
    db: Session,
    row: ProjectAutonomyRun,
    *,
    artifacts: Iterable[ProjectAutonomyArtifact] | None = None,
) -> dict[str, Any]:
    learning = _json_load(row.learning_json, {})
    packet = _quality_bar_from_mapping(learning)
    if packet:
        return packet

    agents = _json_load(row.agents_json, [])
    agent_items = agents if isinstance(agents, list) else [agents]
    for agent in agent_items:
        packet = _quality_bar_from_mapping(agent)
        if packet:
            return packet

    artifact_rows = artifacts
    if artifact_rows is None:
        artifact_rows = (
            db.query(ProjectAutonomyArtifact)
            .filter(ProjectAutonomyArtifact.run_id == row.run_id)
            .order_by(ProjectAutonomyArtifact.id.desc())
            .limit(24)
            .all()
        )
    for artifact in artifact_rows:
        packet = _artifact_quality_bar_payload(artifact)
        if packet:
            return packet
    return {}


def run_payload(db: Session, row: ProjectAutonomyRun, *, include_events: bool = False) -> dict[str, Any]:
    payload = _run_payload(row)
    payload["architect_review"] = _architect_review_payload(_latest_architect_review(db, row.run_id))
    if include_events:
        payload["messages"] = [
            _message_payload(message)
            for message in (
                db.query(ProjectAutonomyMessage)
                .filter(ProjectAutonomyMessage.run_id == row.run_id)
                .order_by(ProjectAutonomyMessage.id.asc())
                .limit(300)
                .all()
            )
        ]
        payload["steps"] = [
            _step_payload(step)
            for step in (
                db.query(ProjectAutonomyStep)
                .filter(ProjectAutonomyStep.run_id == row.run_id)
                .order_by(ProjectAutonomyStep.id.asc())
                .limit(300)
                .all()
            )
        ]
        artifacts = (
            db.query(ProjectAutonomyArtifact)
            .filter(ProjectAutonomyArtifact.run_id == row.run_id)
            .order_by(ProjectAutonomyArtifact.id.asc())
            .limit(80)
            .all()
        )
        payload["artifacts"] = [
            _artifact_payload(artifact)
            for artifact in artifacts
        ]
        payload["scheduled_quality"] = _scheduled_quality_payload(db, row, artifacts=artifacts)
        payload["quality_bar"] = _quality_bar_payload(db, row, artifacts=artifacts)
    else:
        payload["scheduled_quality"] = _scheduled_quality_payload(db, row)
        payload["quality_bar"] = _quality_bar_payload(db, row)
    return payload


def list_runs(
    db: Session,
    *,
    user_id: int | None = None,
    repo_id: int | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    recover_orphaned_runs(db, user_id=user_id)
    q = db.query(ProjectAutonomyRun)
    if user_id is not None:
        q = q.filter(ProjectAutonomyRun.user_id == user_id)
    if repo_id is not None:
        q = q.filter(ProjectAutonomyRun.repo_id == int(repo_id))
    rows = q.order_by(ProjectAutonomyRun.created_at.desc(), ProjectAutonomyRun.id.desc()).limit(limit).all()
    return [run_payload(db, row, include_events=False) for row in rows]


def get_run(
    db: Session,
    run_id: str,
    *,
    user_id: int | None = None,
    include_events: bool = True,
) -> dict[str, Any] | None:
    recover_orphaned_runs(db, user_id=user_id)
    row = _get_run_row(db, run_id, user_id=user_id)
    if row is None:
        return None
    return run_payload(db, row, include_events=include_events)


def _latest_architect_review(db: Session, run_id: str) -> ProjectAutonomyArchitectReview | None:
    return (
        db.query(ProjectAutonomyArchitectReview)
        .filter(ProjectAutonomyArchitectReview.run_id == run_id)
        .order_by(ProjectAutonomyArchitectReview.id.desc())
        .first()
    )


def recover_orphaned_runs(db: Session, *, user_id: int | None = None) -> int:
    q = db.query(ProjectAutonomyRun).filter(ProjectAutonomyRun.status.in_(tuple(ACTIVE_STATUSES)))
    if user_id is not None:
        q = q.filter(ProjectAutonomyRun.user_id == user_id)
    rows = q.all()
    recovered = 0
    for row in rows:
        last_seen = row.updated_at or row.started_at or row.created_at
        if last_seen and last_seen >= _PROCESS_STARTED_AT:
            continue
        _finish(
            db,
            row,
            status="blocked",
            stage=row.current_stage or "interrupted",
            title="Autopilot worker interrupted",
            error_message=(
                "Autopilot worker was interrupted by an API restart before this durable run completed. "
                "Start a new run so the current process can own the worktree safely."
            ),
            merge_status="blocked",
            merge_message="Worker interrupted by API restart.",
        )
        release_run_leases(db, row.run_id)
        recovered += 1
    if recovered:
        db.commit()
    return recovered


def events_after(
    db: Session,
    run_id: str,
    *,
    after_message_id: int = 0,
    after_step_id: int = 0,
    after_artifact_id: int = 0,
) -> dict[str, Any]:
    messages = (
        db.query(ProjectAutonomyMessage)
        .filter(ProjectAutonomyMessage.run_id == run_id, ProjectAutonomyMessage.id > int(after_message_id or 0))
        .order_by(ProjectAutonomyMessage.id.asc())
        .limit(100)
        .all()
    )
    steps = (
        db.query(ProjectAutonomyStep)
        .filter(ProjectAutonomyStep.run_id == run_id, ProjectAutonomyStep.id > int(after_step_id or 0))
        .order_by(ProjectAutonomyStep.id.asc())
        .limit(100)
        .all()
    )
    artifacts = (
        db.query(ProjectAutonomyArtifact)
        .filter(ProjectAutonomyArtifact.run_id == run_id, ProjectAutonomyArtifact.id > int(after_artifact_id or 0))
        .order_by(ProjectAutonomyArtifact.id.asc())
        .limit(50)
        .all()
    )
    return {
        "messages": [_message_payload(message) for message in messages],
        "steps": [_step_payload(step) for step in steps],
        "artifacts": [_artifact_payload(artifact) for artifact in artifacts],
        "after_message_id": max([int(message.id) for message in messages], default=int(after_message_id or 0)),
        "after_step_id": max([int(step.id) for step in steps], default=int(after_step_id or 0)),
        "after_artifact_id": max([int(artifact.id) for artifact in artifacts], default=int(after_artifact_id or 0)),
    }


def _get_run_row(db: Session, run_id: str, *, user_id: int | None = None) -> ProjectAutonomyRun | None:
    q = db.query(ProjectAutonomyRun).filter(ProjectAutonomyRun.run_id == run_id)
    if user_id is not None:
        q = q.filter(ProjectAutonomyRun.user_id == user_id)
    return q.first()


def _resolve_repo_for_run(db: Session, repo_id: int | None, *, user_id: int | None) -> CodeRepo | None:
    if repo_id is not None:
        return cb_indexer.get_accessible_repo(
            db,
            int(repo_id),
            user_id=user_id,
            include_shared=True,
        )
    return workspace_mod.first_reachable_workspace_repo(db, user_id=user_id)


def create_run(
    db: Session,
    *,
    prompt: str,
    repo_id: int | None = None,
    user_id: int | None = None,
    autonomy_level: str = "full_local",
    model_policy: str = "local_first",
    execution_mode: str = EXECUTION_MODE_PLAN_APPROVAL,
    start_planning: bool = False,
    attachments: Any = None,
) -> ProjectAutonomyRun:
    clean_prompt = (prompt or "").strip()
    clean_attachments = _normalise_message_attachments(attachments)
    if not clean_prompt and clean_attachments:
        clean_prompt = _attachment_display_text(clean_attachments)
    if not clean_prompt:
        raise ValueError("Prompt is required.")
    repo = _resolve_repo_for_run(db, repo_id, user_id=user_id)
    if repo is None:
        raise ValueError("No reachable registered repo is available for Project Autopilot.")
    if resolve_repo_runtime_path(repo) is None:
        raise ValueError("The selected repo is registered but not reachable from this runtime.")
    allowed_execution_modes = {EXECUTION_MODE_PLAN_APPROVAL, EXECUTION_MODE_FULL_AUTOPILOT}
    clean_execution_mode = (
        execution_mode if execution_mode in allowed_execution_modes else EXECUTION_MODE_PLAN_APPROVAL
    )
    initial_status = RUN_STATUS_QUEUED if start_planning else RUN_STATUS_CHATTING
    initial_stage = STAGE_QUEUED if start_planning else STAGE_CHAT
    initial_plan_status = PLAN_STATUS_DRAFTING if start_planning else RUN_STATUS_CHATTING

    run_id = "pa_" + uuid.uuid4().hex[:14]
    project_run = start_run(
        db,
        AUTONOMOUS_KIND,
        user_id=user_id,
        repo_id=int(repo.id),
        trigger_source="project_autopilot",
        title="Autopilot queued",
        detail={
            "run_id": run_id,
            "prompt_preview": clean_prompt[:200],
            "repo_name": repo.name,
        },
    )
    row = ProjectAutonomyRun(
        run_id=run_id,
        project_run_id=project_run.id,
        user_id=user_id,
        repo_id=int(repo.id),
        prompt=clean_prompt,
        status=initial_status,
        current_stage=initial_stage,
        autonomy_level=autonomy_level,
        execution_mode=clean_execution_mode,
        plan_status=initial_plan_status,
        chat_title=clean_prompt[:120],
        model_policy=model_policy,
        merge_status=MERGE_STATUS_PENDING,
    )
    db.add(row)
    db.flush()
    _record_step(
        db,
        row,
        initial_stage,
        "Autopilot run queued" if start_planning else "Autopilot chat opened",
        status="completed",
        detail={"repo_id": int(repo.id), "repo_name": repo.name},
        commit=False,
    )
    _add_artifact(
        db,
        row.run_id,
        "prompt",
        "operator_prompt",
        content=clean_prompt,
        content_json={"attachments": clean_attachments} if clean_attachments else None,
        commit=False,
    )
    if clean_attachments:
        _record_attachment_artifacts(db, row, clean_attachments, source="initial_prompt")
    prompt_metadata: dict[str, Any] = {
        "repo_id": int(repo.id),
        "repo_name": repo.name,
    }
    if clean_attachments:
        prompt_metadata["attachments"] = clean_attachments
    _record_message(
        db,
        row,
        "user",
        clean_prompt,
        message_type="prompt",
        metadata=prompt_metadata,
        commit=False,
    )
    if not start_planning:
        _record_message(
            db,
            row,
            "assistant",
            _initial_chat_reply(clean_prompt),
            message_type="chat",
            metadata={"repo_id": int(repo.id), "repo_name": repo.name},
            commit=False,
        )
    db.commit()
    db.refresh(row)
    return row


def request_cancel(db: Session, run_id: str, *, user_id: int | None = None) -> dict[str, Any] | None:
    row = _get_run_row(db, run_id, user_id=user_id)
    if row is None:
        return None
    row.cancel_requested = True
    if row.status in IDLE_STATUSES:
        _finish(
            db,
            row,
            status="cancelled",
            stage=row.current_stage or "cancelled",
            title="Autopilot cancelled",
            error_message="Run cancelled by operator.",
            merge_status="cancelled",
            merge_message="Cancelled before implementation.",
        )
        db.refresh(row)
        return run_payload(db, row, include_events=True)
    if row.status in ACTIVE_STATUSES:
        _record_step(
            db,
            row,
            row.current_stage or "cancel",
            "Cancel requested",
            status="completed",
            detail={"requested_at": _utcnow().isoformat()},
            commit=False,
        )
    db.commit()
    db.refresh(row)
    return run_payload(db, row, include_events=True)


def merge_run(db: Session, run_id: str, *, user_id: int | None = None) -> dict[str, Any] | None:
    row = _get_run_row(db, run_id, user_id=user_id)
    if row is None:
        return None
    if row.status != RUN_STATUS_COMPLETED:
        row.merge_status = "blocked"
        row.merge_message = "Manual merge is allowed only after the autonomy run completes validation."
        db.commit()
        return run_payload(db, row, include_events=True) | {
            "merge_result": {"ok": False, "reason": row.merge_message}
        }
    if not row.integration_branch:
        row.merge_status = "blocked"
        row.merge_message = "No integration branch is recorded for this run."
        db.commit()
        return run_payload(db, row, include_events=True)
    repo = _repo_for_row(db, row)
    repo_path = resolve_repo_runtime_path(repo) if repo is not None else None
    if repo is None or repo_path is None:
        row.merge_status = "blocked"
        row.merge_message = "Selected repo is no longer reachable."
        db.commit()
        return run_payload(db, row, include_events=True)
    changed_files = [str(x) for x in _json_load(row.files_json, [])]
    try:
        result = _attempt_merge(db, row, repo_path, changed_files)
        db.commit()
        return run_payload(db, row, include_events=True) | {"merge_result": result}
    finally:
        release_run_leases(db, row.run_id)
        db.commit()


def append_user_message(
    db: Session,
    run_id: str,
    *,
    content: str,
    user_id: int | None = None,
    attachments: Any = None,
) -> dict[str, Any] | None:
    row = _get_run_row(db, run_id, user_id=user_id)
    if row is None:
        return None
    clean_attachments = _normalise_message_attachments(attachments)
    clean = (content or "").strip()
    display_content = clean or _attachment_display_text(clean_attachments)
    if not display_content:
        raise ValueError("Message is required.")
    _record_message(
        db,
        row,
        "user",
        display_content,
        metadata={"attachments": clean_attachments} if clean_attachments else None,
        commit=False,
    )
    if clean_attachments:
        _record_attachment_artifacts(db, row, clean_attachments, source="chat_message")
    if row.status == RUN_STATUS_CHATTING:
        if _looks_like_plan_start_prompt(display_content):
            _mark_plan_requested(db, row)
        else:
            reply = _chat_reply(db, row, display_content)
            _record_message(db, row, "assistant", reply, message_type="chat", commit=False)
    if (
        row.plan_status in {
            PLAN_STATUS_AWAITING_APPROVAL,
            PLAN_STATUS_AWAITING_CLARIFICATION,
            PLAN_STATUS_REVISING,
        }
        and row.status in {RUN_STATUS_AWAITING_APPROVAL, RUN_STATUS_AWAITING_CLARIFICATION}
    ):
        row.status = RUN_STATUS_QUEUED
        row.plan_status = PLAN_STATUS_REVISING
        row.current_stage = "plan"
        row.prompt = _conversation_prompt(db, row)
        row.plan_json = "{}"
        row.files_json = "[]"
        row.agents_json = "[]"
        _record_architect_review_invalidated_by_feedback(db, row, display_content)
        _record_message(
            db,
            row,
            "assistant",
            "I'll revise the plan with that feedback before making any code changes. The previous approval is no longer valid.",
            message_type="status",
            commit=False,
        )
    db.commit()
    db.refresh(row)
    return run_payload(db, row, include_events=True)


def approve_plan(db: Session, run_id: str, *, user_id: int | None = None) -> dict[str, Any] | None:
    row = _get_run_row(db, run_id, user_id=user_id)
    if row is None:
        return None
    if row.status != RUN_STATUS_AWAITING_APPROVAL or row.plan_status != PLAN_STATUS_AWAITING_APPROVAL:
        raise ValueError("This run is not waiting on an approval-ready architect plan.")
    plan = _json_load(row.plan_json, {})
    if not plan:
        raise ValueError("This run does not have a plan to approve yet.")
    review = _latest_architect_review(db, row.run_id)
    if not _architect_review_passed(review):
        raise ValueError("Architect quality gate has not passed. Revise the plan before approval.")
    row.plan_status = PLAN_STATUS_APPROVED
    row.status = RUN_STATUS_QUEUED
    row.current_stage = STAGE_IMPLEMENT
    _record_message(
        db,
        row,
        "assistant",
        "Plan approved. I'm starting implementation in an isolated worktree now.",
        message_type="status",
        commit=False,
    )
    db.commit()
    db.refresh(row)
    return run_payload(db, row, include_events=True)


def start_plan(db: Session, run_id: str, *, user_id: int | None = None) -> dict[str, Any] | None:
    row = _get_run_row(db, run_id, user_id=user_id)
    if row is None:
        return None
    idle_or_terminal_restartable = {
        RUN_STATUS_CHATTING,
        RUN_STATUS_AWAITING_APPROVAL,
        RUN_STATUS_AWAITING_CLARIFICATION,
        RUN_STATUS_BLOCKED,
        RUN_STATUS_CANCELLED,
    }
    if row.status not in idle_or_terminal_restartable and row.status in ACTIVE_STATUSES:
        raise ValueError("This Autopilot run is already working.")
    _mark_plan_requested(db, row)
    db.commit()
    db.refresh(row)
    return run_payload(db, row, include_events=True)


def _mark_plan_requested(db: Session, row: ProjectAutonomyRun) -> None:
    row.prompt = _conversation_prompt(db, row)
    row.status = RUN_STATUS_QUEUED
    row.current_stage = STAGE_QUEUED
    row.plan_status = PLAN_STATUS_DRAFTING
    row.merge_status = MERGE_STATUS_PENDING
    row.error_message = None
    row.merge_message = None
    row.cancel_requested = False
    _record_step(
        db,
        row,
        STAGE_QUEUED,
        "Autopilot plan requested",
        status="completed",
        detail={"prompt_preview": row.prompt[:240], "repo_id": row.repo_id},
        commit=False,
    )
    _record_message(
        db,
        row,
        "assistant",
        "Got it. I’ll scan the repo and draft a plan, then wait for your approval before editing files.",
        message_type="status",
        commit=False,
    )


def record_visual_validation(
    db: Session,
    run_id: str,
    *,
    kind: str,
    path: str | None = None,
    url: str | None = None,
    note: str | None = None,
    user_id: int | None = None,
) -> dict[str, Any] | None:
    row = _get_run_row(db, run_id, user_id=user_id)
    if row is None:
        return None
    clean_kind = _clean_visual_kind(kind)
    clean_path, path_reject_reason = _sanitize_visual_evidence_path(clean_kind, path)
    clean_url, url_reject_reason = _sanitize_visual_evidence_url(clean_kind, url)
    clean_note = (note or "").strip()
    artifact_type = _visual_artifact_type(clean_kind)
    evidence_available = bool(clean_path or clean_url)
    skipped = not evidence_available
    source = (
        VISUAL_EVIDENCE_SOURCE_DESKTOP
        if clean_path
        else VISUAL_EVIDENCE_SOURCE_URL
        if clean_url
        else VISUAL_EVIDENCE_SOURCE_NONE
    )
    payload = {
        "kind": clean_kind,
        "source": source,
        "path": clean_path or None,
        "url": clean_url or None,
        "note": clean_note or None,
        "skipped": skipped,
        "skip_reason": _visual_skip_reason(clean_kind, path_reject_reason, url_reject_reason) if skipped else None,
        "path_rejected": bool(path and path_reject_reason),
        "path_reject_reason": path_reject_reason,
        "url_rejected": bool(url and url_reject_reason),
        "url_reject_reason": url_reject_reason,
    }
    _add_artifact(db, row.run_id, artifact_type, f"{clean_kind}_evidence", content_json=payload, commit=False)
    if clean_kind == VISUAL_KIND_SCREENSHOT:
        review = (
            "UI evidence attached. The UI and UX agents can use this screenshot when reviewing the run."
            if evidence_available
            else payload["skip_reason"] or VISUAL_SCREENSHOT_UNAVAILABLE_REASON
        )
        _add_artifact(
            db,
            row.run_id,
            "ui_review",
            "ui_agent_review",
            content_json={"summary": review, "evidence_type": artifact_type, "path": clean_path or None},
            commit=False,
        )
    else:
        review = (
            "Video evidence attached for UI/UX validation."
            if evidence_available
            else f"{payload['skip_reason']} This does not block code validation."
        )
        _add_artifact(
            db,
            row.run_id,
            "ux_review",
            "ux_agent_review",
            content_json={"summary": review, "evidence_type": artifact_type, "path": clean_path or None},
            commit=False,
        )
    _record_message(db, row, "assistant", review, message_type="validation", metadata=payload, commit=False)
    db.commit()
    db.refresh(row)
    return run_payload(db, row, include_events=True)


def _conversation_prompt(db: Session, run: ProjectAutonomyRun) -> str:
    rows = (
        db.query(ProjectAutonomyMessage)
        .filter(ProjectAutonomyMessage.run_id == run.run_id, ProjectAutonomyMessage.role == "user")
        .order_by(ProjectAutonomyMessage.id.asc())
        .limit(20)
        .all()
    )
    if not rows:
        return run.prompt
    parts = []
    for idx, row in enumerate(rows):
        part = f"User message {idx + 1}: {row.content}"
        attachment_context = _attachment_context(_message_attachments_from_metadata(row.metadata_json))
        if attachment_context:
            part = f"{part}\n{attachment_context}"
        parts.append(part)
    return "\n\n".join(parts)


def _record_step(
    db: Session,
    run: ProjectAutonomyRun,
    stage: str,
    title: str,
    *,
    status: str = "running",
    agent_name: str = "architect",
    detail: dict[str, Any] | None = None,
    commit: bool = True,
) -> ProjectAutonomyStep:
    idx = (
        db.query(ProjectAutonomyStep)
        .filter(ProjectAutonomyStep.run_id == run.run_id)
        .count()
    )
    now = _utcnow()
    step = ProjectAutonomyStep(
        run_id=run.run_id,
        step_index=idx + 1,
        stage=stage,
        agent_name=agent_name,
        status=status,
        title=title,
        detail_json=_json_text(detail or {}),
        started_at=now,
        finished_at=now if status in {"completed", "failed", "blocked", "cancelled"} else None,
    )
    db.add(step)
    run.current_stage = stage
    run.updated_at = now
    _sync_project_run(db, run, title=title)
    db.flush()
    if commit:
        db.commit()
    return step


def _add_artifact(
    db: Session,
    run_id: str,
    artifact_type: str,
    name: str,
    *,
    content: str | None = None,
    content_json: Any | None = None,
    commit: bool = True,
) -> ProjectAutonomyArtifact:
    text_json = _json_text(content_json) if content_json is not None else None
    length = len((content or text_json or "").encode("utf-8", errors="replace"))
    row = ProjectAutonomyArtifact(
        run_id=run_id,
        artifact_type=artifact_type,
        name=name,
        content=content,
        content_json=text_json,
        byte_length=length,
    )
    db.add(row)
    db.flush()
    if commit:
        db.commit()
    return row


def _record_message(
    db: Session,
    run: ProjectAutonomyRun,
    role: str,
    content: str,
    *,
    message_type: str = "chat",
    metadata: dict[str, Any] | None = None,
    commit: bool = True,
) -> ProjectAutonomyMessage:
    row = ProjectAutonomyMessage(
        run_id=run.run_id,
        role=role,
        message_type=message_type,
        content=content.strip(),
        metadata_json=_json_text(metadata or {}),
    )
    db.add(row)
    run.updated_at = _utcnow()
    db.flush()
    if commit:
        db.commit()
    return row


def _path_tokens(path: str) -> set[str]:
    return {
        token
        for token in re.split(r"[^a-z0-9]+", path.lower())
        if len(token) >= 3
    }


def _prompt_tokens(prompt: str) -> set[str]:
    stop = {
        "and",
        "for",
        "the",
        "this",
        "that",
        "with",
        "from",
        "your",
        "you",
        "are",
        "into",
        "then",
        "please",
        "user",
        "message",
    }
    return {
        token
        for token in re.split(r"[^a-z0-9]+", prompt.lower())
        if len(token) >= 3 and token not in stop
    }


def _dimension(score: int, reason: str) -> dict[str, Any]:
    return {"score": max(0, min(100, int(score))), "reason": reason}


def _review_confidence(score: int, blockers: list[str]) -> str:
    if blockers or score < ARCHITECT_REVIEW_PASSING_SCORE:
        return "low" if score < 70 else "medium"
    if score >= 95:
        return "very_high"
    return "high"


def _plan_file_description(file: dict[str, Any]) -> str:
    return str(file.get("description") or "").strip()


def _plan_mentions_generic_file_pick(plan: dict[str, Any]) -> bool:
    haystack = " ".join(
        [
            str(plan.get("analysis") or ""),
            str(plan.get("notes") or ""),
            " ".join(_plan_file_description(item) for item in plan.get("files") or [] if isinstance(item, dict)),
        ]
    ).lower()
    generic_markers = (
        "small support file",
        "because the request was vague",
        "selected a very large file",
        "directly responds to the operator request",
    )
    return any(marker in haystack for marker in generic_markers)


def _selected_file_rationale(prompt: str, rel: str, description: str) -> str:
    lower = prompt.lower()
    if _is_autopilot_cockpit_request(prompt) and rel == DESKTOP_AUTOPILOT_COCKPIT_FILE:
        return _autopilot_cockpit_reason(prompt)
    if rel == DESKTOP_AUTOPILOT_PRESENTER_FILE:
        return "Autopilot plan presentation is shown by the desktop presenter."
    if rel == DESKTOP_AUTOPILOT_COCKPIT_FILE:
        return "The desktop Autopilot cockpit layout and controls live in this screen."
    if rel == DESKTOP_API_CLIENT_FILE:
        return "The request mentions API, backend, network, or response handling."
    if rel == DESKTOP_NETWORK_ERROR_FILE:
        return "The request is about desktop-visible network or backend error copy."
    if any(token in lower for token in _path_tokens(rel)):
        return "The file path shares concrete terms with the operator request."
    if description:
        return "The plan description ties this file to the requested behavior."
    return "No strong rationale was found for this file."


def _expected_plan_files_for_prompt(prompt: str) -> set[str]:
    prompt_lower = (prompt or "").lower()
    if _is_autopilot_cockpit_request(prompt):
        return {DESKTOP_AUTOPILOT_COCKPIT_FILE}
    if _has_any_token(prompt_lower, BROAD_DESKTOP_DETAIL_TOKENS) and not _has_broad_desktop_plan_override(prompt_lower):
        return {DESKTOP_API_CLIENT_FILE, DESKTOP_NETWORK_ERROR_FILE}
    if _is_broad_desktop_enhancement_request(prompt):
        return set(DESKTOP_AUTOPILOT_PLAN_FILES)
    return set()


def _architect_alternatives(context: dict[str, Any], repo_path: Path | None, prompt: str, selected: list[str]) -> list[dict[str, Any]]:
    selected_set = set(selected)
    alternatives: list[dict[str, Any]] = []
    for rel in _rank_fallback_files(_plan_candidate_files(context, repo_path, prompt), repo_path, prompt):
        if rel in selected_set:
            continue
        alternatives.append(
            {
                "path": rel,
                "reason": _selected_file_rationale(prompt, rel, ""),
            }
        )
        if len(alternatives) >= 4:
            break
    return alternatives


def _review_architect_plan(
    *,
    plan: dict[str, Any],
    files: list[dict[str, Any]],
    context: dict[str, Any],
    repo_path: Path | None,
    prompt: str,
    attempt_index: int,
) -> dict[str, Any]:
    selected = [str(item.get("path") or "") for item in files if item.get("path")]
    descriptions = [_plan_file_description(item) for item in files]
    prompt_lower = prompt.lower()
    blockers: list[str] = []

    if not selected:
        blockers.append("no_concrete_file")
    if len(selected) > _MAX_FILES_PER_EDIT:
        blockers.append("broad_rewrite_without_justification")

    missing = [path for path in selected if not _candidate_exists(repo_path, path)]
    if missing:
        blockers.append("file_missing")

    unsafe_markers = ("drop table", "delete database", "rm -rf", "wipe", "destroy", "real data", "production migration")
    if any(marker in prompt_lower for marker in unsafe_markers):
        blockers.append("unsafe_or_destructive_action")

    broad_desktop = _is_broad_desktop_enhancement_request(prompt)
    detailed_network = _has_any_token(prompt_lower, BROAD_DESKTOP_DETAIL_TOKENS) and not _has_broad_desktop_plan_override(prompt_lower)
    if broad_desktop and any(path in {DESKTOP_API_CLIENT_FILE, DESKTOP_NETWORK_ERROR_FILE} for path in selected):
        blockers.append("mismatched_domain")
    if detailed_network and selected and not any(path in {DESKTOP_API_CLIENT_FILE, DESKTOP_NETWORK_ERROR_FILE} for path in selected):
        blockers.append("mismatched_domain")
    if _plan_mentions_generic_file_pick(plan):
        blockers.append("vague_file_rationale")

    expected_files = _expected_plan_files_for_prompt(prompt)
    selected_set = set(selected)
    matches_expected = bool(expected_files & selected_set)
    if expected_files and selected and not matches_expected and "mismatched_domain" not in blockers:
        blockers.append("mismatched_domain")

    selected_overlap = 0
    request_terms = _prompt_tokens(prompt)
    for path in selected:
        selected_overlap += len(request_terms & _path_tokens(path))
    has_known_good_desktop = broad_desktop and any(path in DESKTOP_AUTOPILOT_PLAN_FILES for path in selected)
    has_known_good_network = detailed_network and any(path in {DESKTOP_API_CLIENT_FILE, DESKTOP_NETWORK_ERROR_FILE} for path in selected)

    intent_fit = 96 if matches_expected else 95 if has_known_good_desktop or has_known_good_network else 74
    if selected_overlap:
        intent_fit = max(intent_fit, 88)
    if not selected:
        intent_fit = 0
    if "mismatched_domain" in blockers:
        intent_fit = 35

    file_relevance = 97 if matches_expected else 96 if has_known_good_desktop or has_known_good_network else 76
    if selected_overlap:
        file_relevance = max(file_relevance, 86)
    if missing:
        file_relevance = 25

    scope_control = 96 if 1 <= len(selected) <= 2 else 45
    analysis_text = str(plan.get("analysis") or "").strip()
    specificity = 92 if all(len(desc) >= 24 for desc in descriptions) and descriptions else 78
    if specificity < 85 and len(analysis_text) >= 20 and selected:
        specificity = 86
    if "vague_file_rationale" in blockers:
        specificity = min(specificity, 60)
    validation_readiness = 90 if selected and all(Path(path).suffix in {".py", ".dart", ".js", ".ts", ".tsx", ".jsx", ".html", ".css"} for path in selected) else 72
    risk_safety = 30 if "unsafe_or_destructive_action" in blockers else 92
    user_value = 92 if descriptions and not _plan_mentions_generic_file_pick(plan) else 72
    if user_value < 85 and len(analysis_text) >= 20 and selected:
        user_value = 86
    if has_known_good_desktop or has_known_good_network:
        user_value = max(user_value, 90)

    dimensions = {
        "intent_fit": _dimension(intent_fit, "Does the selected work match the operator intent?"),
        "file_relevance": _dimension(file_relevance, "Are selected files defensible for this request?"),
        "scope_control": _dimension(scope_control, "Is the plan narrow enough for a safe local run?"),
        "implementation_specificity": _dimension(specificity, "Does the plan say what will change?"),
        "validation_readiness": _dimension(validation_readiness, "Can the repo run meaningful validation after edits?"),
        "risk_safety": _dimension(risk_safety, "Does the plan avoid unsafe or destructive actions?"),
        "user_value": _dimension(user_value, "Will the plan visibly improve the requested workflow?"),
    }
    score = min(item["score"] for item in dimensions.values())
    if score < ARCHITECT_REVIEW_PASSING_SCORE and "low_score" not in blockers:
        blockers.append("low_score")
    status = ARCHITECT_REVIEW_STATUS_PASSED if not blockers and score >= ARCHITECT_REVIEW_PASSING_SCORE else ARCHITECT_REVIEW_STATUS_FAILED
    selected_files = [
        {
            "path": path,
            "rationale": _selected_file_rationale(prompt, path, descriptions[index] if index < len(descriptions) else ""),
        }
        for index, path in enumerate(selected)
    ]
    alternatives = _architect_alternatives(context, repo_path, prompt, selected)
    critique = {
        "blockers": blockers,
        "summary": (
            "Architect review passed."
            if status == ARCHITECT_REVIEW_STATUS_PASSED
            else "Plan needs revision before implementation."
        ),
        "next_action": (
            "approval_ready"
            if status == ARCHITECT_REVIEW_STATUS_PASSED
            else "revise_plan"
        ),
    }
    blocking_reason = None
    if status != ARCHITECT_REVIEW_STATUS_PASSED:
        blocking_reason = "Plan quality gate failed: " + ", ".join(blockers)
    return {
        "attempt_index": attempt_index,
        "status": status,
        "score": score,
        "confidence": _review_confidence(score, blockers),
        "dimensions": dimensions,
        "alternatives": alternatives,
        "critique": critique,
        "selected_files": selected_files,
        "blocking_reason": blocking_reason,
    }


def _record_architect_review(
    db: Session,
    run: ProjectAutonomyRun,
    review: dict[str, Any],
) -> ProjectAutonomyArchitectReview:
    row = ProjectAutonomyArchitectReview(
        run_id=run.run_id,
        attempt_index=int(review.get("attempt_index") or 1),
        status=str(review.get("status") or ARCHITECT_REVIEW_STATUS_FAILED),
        score=int(review.get("score") or 0),
        confidence=str(review.get("confidence") or "low"),
        dimensions_json=_json_text(review.get("dimensions") or {}),
        alternatives_json=_json_text(review.get("alternatives") or []),
        critique_json=_json_text(review.get("critique") or {}),
        selected_files_json=_json_text(review.get("selected_files") or []),
        blocking_reason=review.get("blocking_reason"),
    )
    db.add(row)
    db.flush()
    payload = _architect_review_payload(row)
    _add_artifact(
        db,
        run.run_id,
        "architect_review",
        f"architect_review_attempt_{row.attempt_index}",
        content_json=payload,
        commit=False,
    )
    return row


def _architect_review_passed(review: ProjectAutonomyArchitectReview | dict[str, Any] | None) -> bool:
    if review is None:
        return False
    if isinstance(review, ProjectAutonomyArchitectReview):
        return review.status == ARCHITECT_REVIEW_STATUS_PASSED and int(review.score or 0) >= ARCHITECT_REVIEW_PASSING_SCORE
    return (
        str(review.get("status") or "") == ARCHITECT_REVIEW_STATUS_PASSED
        and int(review.get("score") or 0) >= ARCHITECT_REVIEW_PASSING_SCORE
    )


def _record_architect_review_invalidated_by_feedback(
    db: Session,
    run: ProjectAutonomyRun,
    feedback: str,
) -> None:
    latest = _latest_architect_review(db, run.run_id)
    latest_payload = _architect_review_payload(latest)
    selected_files = latest_payload.get("selected_files") or []
    _record_architect_review(
        db,
        run,
        {
            "attempt_index": int(latest_payload.get("attempt_index") or 0) + 1,
            "status": ARCHITECT_REVIEW_STATUS_NEEDS_REVISION,
            "score": 0,
            "confidence": "low",
            "dimensions": {},
            "alternatives": latest_payload.get("alternatives") or [],
            "critique": {
                "blockers": ["operator_feedback"],
                "summary": "Operator feedback invalidated the previous plan before implementation.",
                "next_action": "revise_plan",
                "feedback_preview": _clip(feedback, 240),
            },
            "selected_files": selected_files,
            "blocking_reason": "Operator feedback changed the requirements; the previous plan must be revised and reviewed again.",
        },
    )


def _revise_plan_from_review(
    plan: dict[str, Any],
    review: dict[str, Any],
    context: dict[str, Any],
    repo_path: Path | None,
    prompt: str,
) -> dict[str, Any] | None:
    blockers = set((review.get("critique") or {}).get("blockers") or [])
    if _is_broad_desktop_enhancement_request(prompt):
        revised = _broad_desktop_enhancement_plan(repo_path)
        if revised is not None:
            return revised
    if blockers & {"no_concrete_file", "file_missing", "mismatched_domain", "vague_file_rationale", "low_score"}:
        revised = _fallback_plan_from_context(context, repo_path, prompt, "architect review requested a safer plan")
        if revised.get("files"):
            return revised
    if blockers == {"low_score"}:
        improved = dict(plan)
        improved["notes"] = "Revised after architect review; approval remains gated by the next review."
        return improved
    return None


def _architect_review_summary(review: dict[str, Any]) -> str:
    status = str(review.get("status") or "")
    score = int(review.get("score") or 0)
    confidence = str(review.get("confidence") or "low").replace("_", " ")
    if status == ARCHITECT_REVIEW_STATUS_PASSED:
        return f"Architect quality gate passed at {score}/100 confidence {confidence}."
    reason = str(review.get("blocking_reason") or "Plan quality gate failed.")
    return f"Architect quality gate did not pass ({score}/100): {reason}"


def _clarification_message(review: dict[str, Any]) -> str:
    reason = str(review.get("blocking_reason") or "the plan is not specific enough yet")
    return (
        "I do not have an implementation plan I trust yet. "
        f"{reason}. Tell me the exact behavior or screen you want changed, and I will draft a stronger plan before touching files."
    )


def _plan_message(
    plan: dict[str, Any],
    files: list[dict[str, Any]],
    agents: list[dict[str, Any]],
    review: dict[str, Any] | None = None,
) -> str:
    analysis = _operator_safe_plan_text(plan.get("analysis"))
    notes = _operator_safe_plan_text(plan.get("notes"))
    file_paths = [str(item.get("path") or "") for item in files if item.get("path")]
    agent_names = [str(item.get("name") or "") for item in agents if item.get("name")]
    review_passed = review is None or review.get("status") == ARCHITECT_REVIEW_STATUS_PASSED
    parts = [
        (
            "I drafted a plan and I'm waiting for your approval before changing files."
            if review_passed
            else "I drafted a candidate plan, but the architect quality gate did not pass. I won't ask for approval yet."
        )
    ]
    if analysis:
        parts.append(analysis)
    descriptions = [
        str(item.get("description") or "").strip()
        for item in files
        if str(item.get("description") or "").strip()
    ]
    if descriptions:
        parts.append("Planned change: " + "; ".join(descriptions[:3]) + ".")
    if file_paths:
        parts.append("I expect to work in: " + ", ".join(file_paths[:6]) + ("." if len(file_paths) <= 6 else f", and {len(file_paths) - 6} more."))
    if agent_names:
        parts.append("Lanes: " + ", ".join(agent_names[:6]) + ".")
    if review:
        parts.append(_architect_review_summary(review))
        selected = review.get("selected_files") or []
        if selected:
            rationale = [
                f"{item.get('path')}: {item.get('rationale')}"
                for item in selected[:3]
                if item.get("path") and item.get("rationale")
            ]
            if rationale:
                parts.append("Why these files: " + " ".join(rationale))
        alternatives = review.get("alternatives") or []
        if alternatives:
            rejected = [
                f"{item.get('path')} ({item.get('reason')})"
                for item in alternatives[:3]
                if item.get("path")
            ]
            if rejected:
                parts.append("Alternatives considered: " + "; ".join(rejected) + ".")
    if notes and notes != analysis:
        parts.append(notes)
    parts.append("Send feedback to revise the plan, or approve it to let me implement in an isolated worktree.")
    return "\n\n".join(parts)


def _completion_message(run: ProjectAutonomyRun) -> str:
    status = str(run.status or "")
    if status == "merged":
        return str(run.merge_message or "Implementation is complete and merged safely.")
    if status == "blocked":
        return str(run.merge_message or run.error_message or "Implementation is blocked. Review the inspector for the safe next step.")
    if status == "failed":
        return str(run.error_message or "Implementation failed before completion.")
    if status == "cancelled":
        return "This Autopilot run was cancelled."
    return str(run.merge_message or "Implementation finished.")


def _initial_chat_reply(prompt: str) -> str:
    if _looks_like_live_monitoring_prompt(prompt):
        return (
            "This looks like a live monitoring/debugging request rather than a repo-editing task. "
            "I won't scan or edit the repo for that from Project Autopilot; use the live "
            "operator/chat monitor, or ask for a specific code change."
        )
    if _looks_like_greeting_or_chat(prompt):
        return (
            "Hey, I'm here. We can brainstorm, inspect ideas, or shape a plan together. "
            "I won't scan or edit the repo until you start a plan."
        )
    return (
        "I'm ready to help shape this. We can talk it through here first; when you want me "
            f"to inspect the repo and draft an implementation plan, use {PLAN_START_CHAT_ACTION_LABEL} in the sidebar."
    )


def _brainstorm_context_block(db: Session, run: ProjectAutonomyRun, latest_user_message: str) -> str:
    """What separates an insightful answer from generic filler: the model
    must SEE the project. Repo identity, the brain's accumulated insights,
    recent autonomy activity, and code-search hits for the user's actual
    question — all cheap reads, no LLM."""
    parts: list[str] = []
    try:
        from ...models.code_brain import CodeRepo

        repo = db.query(CodeRepo).filter(CodeRepo.id == run.repo_id).first() if run.repo_id else None
        if repo is None:
            repo = db.query(CodeRepo).order_by(CodeRepo.id).first()
        if repo is not None:
            parts.append(
                f"Project: {repo.name} (path {repo.path}; languages {repo.language_stats or 'n/a'}; "
                f"frameworks {repo.framework_tags or 'n/a'})."
            )
            try:
                from ...services.code_brain.search import search_code

                hits = search_code(db, latest_user_message, repo_id=int(repo.id), limit=6)
                if hits:
                    lines = [
                        f"- {h['file']}:{h.get('line', '?')} {h.get('type', '')} {h.get('symbol', '')}"
                        for h in hits
                    ]
                    parts.append("Code likely relevant to the question:\n" + "\n".join(lines))
            except Exception:
                pass
    except Exception:
        pass
    try:
        insights = [
            str(i.get("description") or "")
            for i in insights_mod.get_insights(db, repo_id=run.repo_id)[:6]
            if i.get("description")
        ]
        if insights:
            parts.append("Project insights the brain has learned:\n- " + "\n- ".join(insights))
    except Exception:
        pass
    try:
        recent_runs = (
            db.query(ProjectAutonomyRun)
            .filter(ProjectAutonomyRun.run_id != run.run_id)
            .order_by(ProjectAutonomyRun.id.desc())
            .limit(5)
            .all()
        )
        if recent_runs:
            parts.append(
                "Recent Autopilot activity:\n- "
                + "\n- ".join(f"{r.run_id}: {_clip(str(r.prompt or ''), 100)} [{r.status}]" for r in recent_runs)
            )
    except Exception:
        pass
    return "\n\n".join(p for p in parts if p)


def _chat_reply(db: Session, run: ProjectAutonomyRun, latest_user_message: str) -> str:
    if _looks_like_greeting_or_chat(latest_user_message):
        return _initial_chat_reply(latest_user_message)
    recent = (
        db.query(ProjectAutonomyMessage)
        .filter(ProjectAutonomyMessage.run_id == run.run_id)
        .order_by(ProjectAutonomyMessage.id.desc())
        .limit(8)
        .all()
    )
    context_block = _brainstorm_context_block(db, run, latest_user_message)
    system = (
        "You are CHILI, the project architect for THIS specific repository. "
        "Ground every answer in the project context below — never answer "
        "generically about other domains. Be concrete: name real files, "
        "modules, and recent activity when relevant. This is a brainstorming "
        "conversation, not an implementation run; do not claim you changed "
        "files. When the user wants something implemented, suggest "
        f"{PLAN_START_CHAT_ACTION_LABEL} in the sidebar.\n\n"
        f"## Project context\n{context_block or '(no repo registered yet)'}"
    )
    messages = [{"role": "system", "content": system}]
    for row in reversed(recent):
        role = "assistant" if row.role == "assistant" else "user"
        content = row.content
        if role == "user":
            attachment_context = _attachment_context(_message_attachments_from_metadata(row.metadata_json))
            if attachment_context:
                content = f"{content}\n{attachment_context}"
        messages.append({"role": role, "content": content})

    # Brain upgrade: route through the gateway cascade (free Groq-70B class
    # first, with the standard fallbacks) instead of the tiny local chat
    # model that answered project questions with generic filler.
    import time as _time

    from ...services.context_brain.llm_gateway import gateway_chat as _gw_chat

    _t0 = _time.monotonic()
    reply_text = ""
    model_used = "gateway_error"
    try:
        gw = _gw_chat(
            messages=messages[1:],
            purpose="brainstorm_chat",
            system_prompt=system,
            trace_id=f"autopilot-brainstorm-{run.run_id}",
            user_message=latest_user_message,
            max_tokens=900,
            # The gateway must NOT share this session: its internal
            # rollback-on-error would discard the caller's uncommitted user
            # message (caught by the suite as a vanished chat message).
            db=None,
        )
        if isinstance(gw, dict):
            reply_text = (gw.get("reply") or "").strip()
            model_used = str(gw.get("model") or model_used)
    except Exception:
        pass
    if not reply_text:
        # Offline fallback: the old local chat model path.
        model_info = select_local_model()
        if model_info.get("model"):
            result = ollama_client.chat(
                messages,
                str(model_info["model"]),
                temperature=0.35,
                timeout_sec=45,
                options={"num_predict": 400, "num_ctx": 4096, "keep_alive": _OLLAMA_KEEP_ALIVE},
            )
            _note_model_call_result(str(model_info["model"]), result)
            if result.ok and result.text.strip():
                reply_text = result.text.strip()
                model_used = str(model_info["model"])
    _add_artifact(
        db,
        run.run_id,
        "model_call",
        "chat_model_call",
        content_json={
            "model": model_used,
            "ok": bool(reply_text),
            "latency_ms": int((_time.monotonic() - _t0) * 1000),
            "purpose": "brainstorm_chat",
            "grounded": bool(context_block),
        },
        commit=False,
    )
    if reply_text:
        return _clip(reply_text, CHAT_REPLY_LIMIT)
    return (
        "I'm here for the brainstorming. The chat models didn't answer cleanly, "
        "but we can still keep the idea moving or start a plan when you're ready."
    )


def _sync_project_run(db: Session, run: ProjectAutonomyRun, *, title: str | None = None) -> None:
    if not run.project_run_id:
        return
    project_run = db.query(ProjectDomainRun).filter(ProjectDomainRun.id == run.project_run_id).first()
    if project_run is None:
        return
    if title:
        project_run.title = title
    project_run.detail_json = _json_text(
        {
            "run_id": run.run_id,
            "status": run.status,
            "stage": run.current_stage,
            "merge_status": run.merge_status,
            "repo_id": run.repo_id,
            "branch": run.integration_branch,
        }
    )


def _finish(
    db: Session,
    run: ProjectAutonomyRun,
    *,
    status: str,
    stage: str,
    title: str,
    error_message: str | None = None,
    merge_status: str | None = None,
    merge_message: str | None = None,
) -> ProjectAutonomyRun:
    now = _utcnow()
    run.status = status
    run.current_stage = stage
    if status in TERMINAL_STATUSES:
        run.plan_status = PLAN_STATUS_IMPLEMENTED if run.plan_status == PLAN_STATUS_APPROVED else run.plan_status
    run.error_message = error_message
    if merge_status is not None:
        run.merge_status = merge_status
    if merge_message is not None:
        run.merge_message = merge_message
    run.finished_at = now
    run.updated_at = now
    _record_step(
        db,
        run,
        stage,
        title,
        status=status if status in {"failed", "blocked", "cancelled"} else "completed",
        detail={"error_message": error_message, "merge_message": merge_message},
        commit=False,
    )
    _record_message(
        db,
        run,
        "assistant",
        _completion_message(run),
        message_type="result",
        metadata={"status": status, "stage": stage, "merge_status": run.merge_status},
        commit=False,
    )
    if run.project_run_id:
        project_run = db.query(ProjectDomainRun).filter(ProjectDomainRun.id == run.project_run_id).first()
        if project_run is not None:
            finish_run(
                db,
                project_run,
                status=status,
                detail={
                    "run_id": run.run_id,
                    "stage": stage,
                    "merge_status": run.merge_status,
                    "merge_message": run.merge_message,
                    "branch": run.integration_branch,
                    "validation": _json_load(run.validation_json, []),
                },
                error_message=error_message,
            )
    db.commit()
    return run


def _repo_for_row(db: Session, row: ProjectAutonomyRun) -> CodeRepo | None:
    if row.repo_id is None:
        return None
    return cb_indexer.get_accessible_repo(
        db,
        int(row.repo_id),
        user_id=row.user_id,
        include_shared=True,
    )


def _check_cancel(db: Session, run: ProjectAutonomyRun) -> None:
    db.refresh(run)
    if run.cancel_requested:
        raise AutonomyCancelled("Run cancelled by operator.")


def _model_cooldown_expired(info: dict[str, Any], now: datetime) -> bool:
    until = info.get("until")
    return not isinstance(until, datetime) or until <= now


def _active_model_cooldowns(now: datetime | None = None) -> dict[str, dict[str, Any]]:
    now = now or _utcnow()
    expired = [model for model, info in _MODEL_COOLDOWNS.items() if _model_cooldown_expired(info, now)]
    for model in expired:
        _MODEL_COOLDOWNS.pop(model, None)
    return dict(_MODEL_COOLDOWNS)


def _model_call_should_cooldown(error: str | None) -> bool:
    lower = (error or "").lower()
    return any(marker in lower for marker in _MODEL_COOLDOWN_ERROR_MARKERS)


def _mark_model_cooldown(model: str | None, error: str | None) -> None:
    clean = (model or "").strip()
    if not clean or not _model_call_should_cooldown(error):
        return
    _MODEL_COOLDOWNS[clean] = {
        "until": _utcnow() + timedelta(seconds=max(1, _MODEL_COOLDOWN_SEC)),
        "reason": _friendly_model_issue(error),
    }


def _clear_model_cooldown(model: str | None) -> None:
    clean = (model or "").strip()
    if clean:
        _MODEL_COOLDOWNS.pop(clean, None)


def _note_model_call_result(model: str | None, result: Any) -> None:
    if bool(getattr(result, "ok", False)):
        _clear_model_cooldown(model)
    else:
        _mark_model_cooldown(model, getattr(result, "error", None))


def select_local_model() -> dict[str, Any]:
    models = ollama_client.list_models()
    skipped = _active_model_cooldowns()
    cooling = set(skipped.keys())
    for preferred in _MODEL_PREFERENCE:
        exact = next((model for model in models if model == preferred), None)
        if exact and exact not in cooling:
            return {
                "model": exact,
                "available": True,
                "installed_models": models,
                "skipped_models": skipped,
                "recommendation": None,
            }
        if ":" in preferred:
            continue
        prefix = f"{preferred}:"
        for model in models:
            if model in cooling:
                continue
            if model == preferred or model.startswith(prefix):
                return {
                    "model": model,
                    "available": True,
                    "installed_models": models,
                    "skipped_models": skipped,
                    "recommendation": None,
                }
    return {
        "model": None,
        "available": False,
        "installed_models": models,
        "skipped_models": skipped,
        "recommendation": "Pull a local coder model, for example: ollama pull qwen2.5-coder:7b",
    }


def _candidate_exists(repo_path: Path | None, rel_path: str) -> bool:
    rel = _safe_rel_path(rel_path)
    if rel is None:
        return False
    if repo_path is None:
        return True
    return (repo_path / rel).is_file()


def _plan_candidate_files(context: dict[str, Any], repo_path: Path | None, prompt: str) -> list[str]:
    prompt_lower = (prompt or "").lower()
    seeded: list[str] = []
    if _is_autopilot_cockpit_request(prompt):
        seeded.extend(DESKTOP_AUTOPILOT_COCKPIT_INTENT_FILES)
    if any(token in prompt_lower for token in ("desktop", "flutter", "native", "ui", "screen", "autopilot")):
        seeded.extend(DESKTOP_SEEDED_FILES)
    if any(token in prompt_lower for token in ("project brain", "project autopilot", "autonomy", "autonomous")):
        seeded.extend(
            [
                "app/services/project_autonomy/orchestrator.py",
                "app/routers/brain_project.py",
                "tests/test_project_autonomy_service.py",
            ]
        )

    context_candidates: list[str] = []
    for item in context.get("relevant_files") or []:
        if isinstance(item, dict):
            context_candidates.append(str(item.get("file") or ""))
    for item in context.get("hotspots") or []:
        if isinstance(item, dict):
            context_candidates.append(str(item.get("file") or ""))

    out: list[str] = []
    seen: set[str] = set()
    for raw in seeded + context_candidates:
        rel = _safe_rel_path(raw)
        if rel is None or rel in seen or not _candidate_exists(repo_path, rel):
            continue
        seen.add(rel)
        out.append(rel)
        if len(out) >= 12:
            break
    return out


def _build_autonomy_plan_prompt(context: dict[str, Any], repo_path: Path | None) -> str:
    request = str(context.get("operator_request") or "")
    candidates = _plan_candidate_files(context, repo_path, request)
    parts = [
        "Return one compact JSON object for a safe local autonomous code run.",
        "No markdown. No prose outside JSON. Keep strings short.",
        "Choose one or two concrete files and avoid speculative rewrites.",
        "",
        "Operator request:",
        request,
        "",
        "Repositories:",
    ]
    for repo in (context.get("repos") or [])[:3]:
        if not isinstance(repo, dict):
            continue
        langs = repo.get("languages") if isinstance(repo.get("languages"), dict) else {}
        lang_bits = ", ".join(f"{k}:{v}" for k, v in list(langs.items())[:5])
        parts.append(
            f"- {repo.get('name')} path={repo.get('runtime_path') or repo.get('path')} "
            f"files={repo.get('file_count')} languages={lang_bits}"
        )

    if candidates:
        parts.extend(["", "Candidate files:"])
        parts.extend(f"- {path}" for path in candidates[:12])

    insights = [
        str(item.get("description") or "")
        for item in (context.get("insights") or [])
        if isinstance(item, dict) and item.get("description")
    ][:6]
    if insights:
        parts.extend(["", "Repo patterns:"])
        parts.extend(f"- {_clip(item, 220)}" for item in insights)

    parts.extend(
        [
            "",
            "JSON shape:",
            '{"analysis":"<=18 words","files":[{"path":"candidate/path","action":"modify","description":"<=12 words"}],"notes":"<=12 words"}',
            "Rules: max 2 files, prefer existing candidate files exactly, include only repo-relative paths.",
        ]
    )
    return _clip("\n".join(parts), _PLAN_PROMPT_CHAR_LIMIT)


def _rank_fallback_files(files: list[str], repo_path: Path | None, prompt: str) -> list[str]:
    prompt_lower = (prompt or "").lower()
    pool = files
    if any(token in prompt_lower for token in ("desktop", "flutter", "native", "ui", "screen")):
        desktop_files = [path for path in files if path.startswith("chili_mobile/")]
        if desktop_files:
            pool = desktop_files

    def sort_key(path: str) -> tuple[int, int, str]:
        if repo_path is None:
            return (1, 0, path)
        try:
            size = (repo_path / path).stat().st_size
        except OSError:
            size = 999_999_999
        should_prefer_network = (
            _has_any_token(prompt_lower, BROAD_DESKTOP_DETAIL_TOKENS)
            and not _has_broad_desktop_plan_override(prompt_lower)
        )
        priority_map = DESKTOP_NETWORK_FILE_PRIORITY if should_prefer_network else DESKTOP_FILE_PRIORITY
        if _is_autopilot_cockpit_request(prompt) and path in DESKTOP_AUTOPILOT_COCKPIT_INTENT_FILES:
            priority_map = {
                DESKTOP_AUTOPILOT_COCKPIT_FILE: 0,
                DESKTOP_AUTOPILOT_PRESENTER_FILE: 2,
            }
        direct = priority_map.get(path, 10)
        return (direct, size, path)

    return sorted(pool, key=sort_key)


def _file_line_count(path: Path) -> int:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def _is_vague_small_request(prompt: str) -> bool:
    prompt_lower = (prompt or "").lower()
    return any(
        phrase in prompt_lower
        for phrase in (
            "small enhancement",
            "find a small",
            "first prompt as a test",
            "quick improvement",
            "tiny improvement",
        )
    )


def _narrow_plan_for_local_model(
    plan: dict[str, Any],
    context: dict[str, Any],
    repo_path: Path | None,
    prompt: str,
) -> dict[str, Any]:
    if not _is_vague_small_request(prompt) or repo_path is None:
        return plan
    raw_files = plan.get("files")
    if not isinstance(raw_files, list):
        return plan

    picks_large_file = False
    for item in raw_files:
        if not isinstance(item, dict):
            continue
        rel = _safe_rel_path(str(item.get("path") or ""))
        if rel is None:
            continue
        full = repo_path / rel
        try:
            size = full.stat().st_size
        except OSError:
            continue
        if size > 50_000 or _file_line_count(full) > 700:
            picks_large_file = True
            break
    if not picks_large_file:
        return plan

    fallback_files = _rank_fallback_files(_plan_candidate_files(context, repo_path, prompt), repo_path, prompt)[:1]
    if not fallback_files:
        return plan
    narrowed = dict(plan)
    narrowed["files"] = [
        {
            "path": fallback_files[0],
            "action": "modify",
            "description": (
                "Make a focused, low-risk desktop usability enhancement in this small support file. "
                f"Operator request: {_clip(prompt, 320)}"
            ),
        }
    ]
    notes = str(narrowed.get("notes") or "")
    narrowed["notes"] = (notes + " " if notes else "") + (
        "Narrowed to a smaller file because the request was vague and the local model selected a very large file."
    )
    return narrowed


def _fallback_file_description(prompt: str, rel: str) -> str:
    if rel == DESKTOP_AUTOPILOT_COCKPIT_FILE and _is_autopilot_cockpit_request(prompt):
        return _autopilot_cockpit_reason(prompt)
    if rel == DESKTOP_AUTOPILOT_PRESENTER_FILE:
        return "Improve human-readable Autopilot plan presentation and approval guidance in the desktop presenter."
    if rel == DESKTOP_AUTOPILOT_COCKPIT_FILE:
        return "Update the desktop Autopilot cockpit controls and chat layout for the requested workflow."
    if rel == DESKTOP_API_CLIENT_FILE:
        return "Improve desktop API response handling for the requested backend or network workflow."
    if rel == DESKTOP_NETWORK_ERROR_FILE:
        return "Improve desktop-visible network error copy for the requested failure mode."
    shared_terms = sorted(_prompt_tokens(prompt) & _path_tokens(rel))
    if shared_terms:
        return "Update this file because its path matches request terms: " + ", ".join(shared_terms[:4]) + "."
    return "Inspect this existing candidate and make only a tightly scoped change if it owns the requested behavior."


def _fallback_plan_from_context(
    context: dict[str, Any],
    repo_path: Path | None,
    prompt: str,
    reason: str,
) -> dict[str, Any]:
    if _is_broad_desktop_enhancement_request(prompt):
        plan = _broad_desktop_enhancement_plan(repo_path)
        if plan is not None:
            return plan
    ranked_files = _rank_fallback_files(_plan_candidate_files(context, repo_path, prompt), repo_path, prompt)
    if _is_vague_small_request(prompt) and repo_path is not None:
        deterministic_files: list[str] = []
        for rel in ranked_files:
            content = _read_file_content(str(repo_path), rel, max_lines=_EDIT_MAX_FILE_LINES)
            if _deterministic_small_desktop_diff(rel, content or "", prompt):
                deterministic_files.append(rel)
        if deterministic_files:
            ranked_files = deterministic_files
    files = ranked_files[:1]
    friendly_reason = _friendly_model_issue(reason)
    if not files:
        return {
            "analysis": f"{friendly_reason} I could not identify a safe candidate file to change.",
            "files": [],
            "notes": "No implementation will start until a safer plan can be drafted.",
        }
    return {
        "analysis": (
            f"{friendly_reason} I used the repo index, the conversation, and known project files to draft "
            "a conservative plan."
        ),
        "files": [
            {
                "path": rel,
                "action": "modify",
                "description": _fallback_file_description(prompt, rel),
            }
            for rel in files
        ],
        "notes": "This fallback plan stays approval-first; generated diffs and validation gates still decide whether the run may merge.",
    }


def build_local_plan(
    db: Session,
    run: ProjectAutonomyRun,
    repo: CodeRepo,
    *,
    context: dict[str, Any] | None = None,
    repo_path: Path | None = None,
) -> dict[str, Any]:
    context = context if context is not None else _gather_context(db, int(repo.id), run.prompt, user_id=run.user_id)
    context["operator_request"] = run.prompt
    repo_path = repo_path if repo_path is not None else resolve_repo_runtime_path(repo)
    if _is_vague_small_request(run.prompt):
        fallback = _fallback_plan_from_context(
            context,
            repo_path,
            run.prompt,
            BROAD_DESKTOP_INTERNAL_REASON
            if _is_broad_desktop_enhancement_request(run.prompt)
            else "vague small request routed to heuristic fast path",
        )
        if fallback.get("files"):
            _add_artifact(db, run.run_id, "plan", "heuristic_plan_fast_path", content_json=fallback)
            return fallback
    model_info = select_local_model()
    if not model_info.get("model"):
        fallback = _fallback_plan_from_context(
            context,
            repo_path,
            run.prompt,
            str(model_info.get("recommendation") or "No local Ollama model is available."),
        )
        _add_artifact(
            db,
            run.run_id,
            "plan",
            "heuristic_plan_fallback",
            content_json={**fallback, "model_selection": model_info},
        )
        if fallback.get("files"):
            return fallback
        raise AutonomyBlocked(str(model_info.get("recommendation") or "No local Ollama model is available."))
    prompt = _build_autonomy_plan_prompt(context, repo_path)
    messages = [
        {
            "role": "system",
            "content": (
                "You are a senior coding architect. Output one compact JSON object only. "
                "Plan for safe autonomous implementation in a git worktree."
            ),
        },
        {"role": "user", "content": prompt},
    ]
    result = ollama_client.chat(
        messages,
        str(model_info["model"]),
        temperature=0.15,
        timeout_sec=_PLAN_TIMEOUT_SEC,
        options={
            "num_predict": _PLAN_NUM_PREDICT,
            "num_ctx": _PLAN_NUM_CTX,
            "keep_alive": _OLLAMA_KEEP_ALIVE,
        },
    )
    _note_model_call_result(str(model_info["model"]), result)
    _add_artifact(
        db,
        run.run_id,
        "model_call",
        "plan_model_call",
        content_json={
            "model": model_info["model"],
            "ok": result.ok,
            "latency_ms": result.latency_ms,
            "error": result.error,
            "installed_models": model_info.get("installed_models"),
            "skipped_models": model_info.get("skipped_models"),
            "prompt_chars": len(prompt),
            "timeout_sec": _PLAN_TIMEOUT_SEC,
            "num_predict": _PLAN_NUM_PREDICT,
            "num_ctx": _PLAN_NUM_CTX,
            "keep_alive": _OLLAMA_KEEP_ALIVE,
        },
    )
    if not result.ok:
        fallback = _fallback_plan_from_context(context, repo_path, run.prompt, result.error or "unknown error")
        _add_artifact(db, run.run_id, "plan", "heuristic_plan_fallback", content_json=fallback)
        if fallback.get("files"):
            return fallback
        raise AutonomyBlocked(f"Local model planning failed: {result.error or 'unknown error'}")
    plan = _parse_plan_json(result.text)
    if not plan:
        fallback = _fallback_plan_from_context(context, repo_path, run.prompt, "unusable model JSON")
        _add_artifact(db, run.run_id, "plan", "heuristic_plan_fallback", content_json=fallback)
        if fallback.get("files"):
            return fallback
        raise AutonomyBlocked("Local model did not return a usable implementation plan.")
    narrowed = _narrow_plan_for_local_model(plan, context, repo_path, run.prompt)
    if narrowed is not plan:
        _add_artifact(db, run.run_id, "plan", "local_model_narrowed_plan", content_json=narrowed)
        plan = narrowed
    plan.setdefault("analysis", "")
    plan.setdefault("files", [])
    plan.setdefault("notes", "")
    return plan


def _plan_files(plan: dict[str, Any]) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in plan.get("files") or []:
        if not isinstance(item, dict):
            continue
        rel = _safe_rel_path(str(item.get("path") or ""))
        if rel is None or rel in seen:
            continue
        seen.add(rel)
        files.append(
            {
                "path": rel,
                "action": str(item.get("action") or "modify"),
                "description": str(item.get("description") or ""),
            }
        )
        if len(files) >= _MAX_FILES_PER_EDIT:
            break
    return files


def assign_agent_lanes(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lanes: dict[str, set[str]] = {"architect": set()}
    for item in files:
        path = str(item.get("path") or "")
        lower = path.lower()
        role = "backend"
        if lower.startswith("tests/") or "/tests/" in lower or Path(lower).name.startswith("test_"):
            role = "qa"
        elif lower.endswith((".html", ".css", ".js", ".jsx", ".ts", ".tsx")) or lower.startswith(("app/static/", "app/templates/")):
            role = "frontend"
        elif any(token in lower for token in ("auth", "token", "secret", "credential", "permission", "security")):
            role = "security"
        elif lower.startswith((".github/", "scripts/", "docker")) or lower in {"docker-compose.yml", "dockerfile"}:
            role = "devops"
        lanes.setdefault(role, set()).add(path)
        lanes["architect"].add(path)

    out: list[dict[str, Any]] = [
        {
            "name": "architect",
            "role": "lead",
            "status": "lead",
            "files": sorted(lanes.get("architect") or []),
        }
    ]
    for role in ("backend", "frontend", "qa", "security", "devops"):
        paths = sorted(lanes.get(role) or [])
        if not paths:
            continue
        out.append({"name": role, "role": role, "status": "assigned", "files": paths})
    return out


def acquire_file_leases(
    db: Session,
    run: ProjectAutonomyRun,
    repo_id: int,
    files: Iterable[str],
    *,
    holder: str = "architect",
    ttl_minutes: int = 120,
) -> list[ProjectAutonomyLease]:
    acquired: list[ProjectAutonomyLease] = []
    now = _utcnow()
    expires_at = _utcnow() + timedelta(minutes=ttl_minutes)
    for raw in files:
        rel = _safe_rel_path(raw)
        if rel is None:
            continue
        lease_key = f"repo:{repo_id}:file:{rel}"
        conflict = (
            db.query(ProjectAutonomyLease)
            .filter(
                ProjectAutonomyLease.repo_id == int(repo_id),
                ProjectAutonomyLease.lease_key == lease_key,
                ProjectAutonomyLease.status == "active",
                ProjectAutonomyLease.run_id != run.run_id,
                or_(ProjectAutonomyLease.expires_at.is_(None), ProjectAutonomyLease.expires_at > now),
            )
            .first()
        )
        if conflict is not None:
            raise AutonomyBlocked(f"File is already leased by another run: {rel}")
        lease = ProjectAutonomyLease(
            run_id=run.run_id,
            repo_id=int(repo_id),
            lease_key=lease_key,
            file_path=rel,
            holder=holder,
            status="active",
            expires_at=expires_at,
        )
        db.add(lease)
        acquired.append(lease)
    db.flush()
    return acquired


def acquire_repo_lease(db: Session, run: ProjectAutonomyRun, repo_id: int) -> ProjectAutonomyLease:
    lease = ProjectAutonomyLease(
        run_id=run.run_id,
        repo_id=int(repo_id),
        lease_key=f"repo:{repo_id}:run:{run.run_id}",
        holder="architect",
        status="active",
        expires_at=_utcnow() + timedelta(minutes=120),
    )
    db.add(lease)
    db.flush()
    return lease


def acquire_agent_file_leases(
    db: Session,
    run: ProjectAutonomyRun,
    repo_id: int,
    agents: list[dict[str, Any]],
) -> list[ProjectAutonomyLease]:
    by_file: dict[str, str] = {}
    for agent in agents:
        name = str(agent.get("name") or "architect")
        if name == "architect":
            continue
        for raw in agent.get("files") or []:
            rel = _safe_rel_path(str(raw))
            if rel and rel not in by_file:
                by_file[rel] = name
    if not by_file:
        architect_files = []
        for agent in agents:
            if str(agent.get("name") or "") == "architect":
                architect_files = [str(x) for x in (agent.get("files") or [])]
                break
        return acquire_file_leases(db, run, repo_id, architect_files, holder="architect")

    leases: list[ProjectAutonomyLease] = []
    for rel, holder in by_file.items():
        leases.extend(acquire_file_leases(db, run, repo_id, [rel], holder=holder))
    return leases


def acquire_merge_lease(db: Session, run: ProjectAutonomyRun, repo_id: int) -> ProjectAutonomyLease:
    lease_key = f"repo:{repo_id}:merge"
    now = _utcnow()
    conflict = (
        db.query(ProjectAutonomyLease)
        .filter(
            ProjectAutonomyLease.repo_id == int(repo_id),
            ProjectAutonomyLease.lease_key == lease_key,
            ProjectAutonomyLease.status == "active",
            ProjectAutonomyLease.run_id != run.run_id,
            or_(ProjectAutonomyLease.expires_at.is_(None), ProjectAutonomyLease.expires_at > now),
        )
        .first()
    )
    if conflict is not None:
        raise AutonomyBlocked("The repo merge lease is held by another autonomy run.")
    lease = ProjectAutonomyLease(
        run_id=run.run_id,
        repo_id=int(repo_id),
        lease_key=lease_key,
        holder="architect",
        status="active",
        expires_at=_utcnow() + timedelta(minutes=30),
    )
    db.add(lease)
    db.flush()
    return lease


def release_run_leases(db: Session, run_id: str) -> None:
    now = _utcnow()
    rows = (
        db.query(ProjectAutonomyLease)
        .filter(ProjectAutonomyLease.run_id == run_id, ProjectAutonomyLease.status == "active")
        .all()
    )
    for row in rows:
        row.status = "released"
        row.released_at = now
    db.flush()


def _local_worktree_root() -> Path:
    raw = (os.environ.get("CHILI_PROJECT_AUTOPILOT_WORKTREE_DIR") or "").strip()
    if raw:
        normalized = raw.replace("\\", "/")
        if os.name == "nt" and (normalized == "/tmp" or normalized.startswith("/tmp/")):
            rel = normalized.removeprefix("/tmp").lstrip("/")
            return Path(tempfile.gettempdir()) / rel if rel else Path(tempfile.gettempdir())
        return Path(raw)
    return Path(tempfile.gettempdir())


def generate_diffs_from_plan(
    db: Session,
    run: ProjectAutonomyRun,
    repo_path: Path,
    files: list[dict[str, Any]],
    *,
    validation_context: str | None = None,
) -> list[str]:
    model_info = select_local_model()
    if not model_info.get("model"):
        raise AutonomyBlocked(str(model_info.get("recommendation") or "No local Ollama model is available."))
    conventions = [
        str(ins.get("description") or "")
        for ins in insights_mod.get_insights(db, repo_id=run.repo_id)[:8]
        if ins.get("description")
    ]
    diffs: list[str] = []
    rejections: list[str] = []

    def try_fallback(rel_path: str, current_content: str | None) -> bool:
        fallback = _deterministic_small_desktop_diff(rel_path, current_content or "", run.prompt)
        if not fallback:
            return False
        check = _git(repo_path, ["apply", "--check"], input_text=fallback, timeout=60)
        if check.returncode != 0:
            rejections.append(
                f"{rel_path}: deterministic fallback did not apply cleanly "
                f"({(check.stderr or check.stdout or '').strip()[:500]})"
            )
            return False
        diffs.append(fallback)
        _add_artifact(
            db,
            run.run_id,
            "diff",
            rel_path,
            content=fallback,
            content_json={"source": "deterministic_small_desktop_fallback"},
        )
        return True

    for item in files:
        _check_cancel(db, run)
        rel = str(item.get("path") or "")
        desc = str(item.get("description") or "")
        content = _read_file_content(str(repo_path), rel, max_lines=_EDIT_MAX_FILE_LINES)
        if validation_context:
            desc = desc + "\n\nValidation failure to repair:\n" + validation_context
        if not validation_context and try_fallback(rel, content):
            continue
        # Proven dispatch-lane edit machinery (SR blocks + fuzzy/indent
        # healing + machine-generated diffs), via the gateway's local-first
        # code tier with cascade escalation — replaces the old direct-Ollama
        # unified-diff guessing against the 260-line truncation, which was
        # the autopilot lane's dominant failure mode.
        import time as _time

        from ...config import settings as _settings
        from ...services.code_brain import agent as code_agent_mod
        from ...services.context_brain.llm_gateway import gateway_chat as _gw_chat

        full_content = code_agent_mod._read_file_full(str(repo_path), rel)
        prompt = code_agent_mod._build_edit_prompt(
            rel,
            code_agent_mod._elide_for_prompt(full_content if full_content is not None else (content or "")),
            desc,
            conventions,
        )
        _t0 = _time.monotonic()
        gw = _gw_chat(
            messages=[{"role": "user", "content": f"Apply the change to {rel} as described."}],
            purpose="code_dispatch_edit",
            system_prompt=prompt,
            trace_id=f"autopilot-edit-{rel}",
            user_message=desc,
            max_tokens=_settings.chili_code_gen_max_tokens,
            # Own session inside the gateway — never share the run session
            # (rollback-on-error would discard pending run state).
            db=None,
        )
        reply_text = (gw.get("reply") or "") if isinstance(gw, dict) else ""
        _add_artifact(
            db,
            run.run_id,
            "model_call",
            f"edit_{rel}",
            content_json={
                "model": (gw.get("model") if isinstance(gw, dict) else None) or "gateway_error",
                "file": rel,
                "ok": bool(reply_text.strip()),
                "latency_ms": int((_time.monotonic() - _t0) * 1000),
                "engine": "sr_blocks_via_gateway",
                "prompt_chars": len(prompt),
            },
        )
        _check_cancel(db, run)
        if not reply_text.strip():
            rejections.append(f"{rel}: model call failed (empty gateway reply)")
            if try_fallback(rel, content):
                continue
            continue
        diff = ""
        sr_blocks = code_agent_mod._parse_search_replace_blocks(reply_text)
        if sr_blocks and full_content is not None:
            outcome = code_agent_mod._apply_search_replace(full_content, sr_blocks)
            if outcome["new_content"] is not None:
                diff = code_agent_mod._unified_diff_text(rel, full_content, outcome["new_content"])
            else:
                _add_artifact(
                    db, run.run_id, "diff_rejected", rel,
                    content_json={"reason": "sr_blocks_rejected", "warnings": outcome["warnings"]},
                )
                rejections.append(
                    f"{rel}: edit blocks rejected ({'; '.join(outcome['warnings'])[:300]})"
                )
                if try_fallback(rel, content):
                    continue
                continue
        if not diff:
            # Legacy fallback: a raw unified diff reply (cloud models).
            diff = _extract_diff(reply_text)
            if diff and diff.lstrip().startswith("@@"):
                diff = f"--- a/{rel}\n+++ b/{rel}\n{diff.lstrip()}"
        if not diff:
            _add_artifact(
                db,
                run.run_id,
                "diff_rejected",
                rel,
                content_json={
                    "reason": "model_response_missing_edit_blocks_and_diff",
                    "response_preview": _clip(reply_text, 800),
                },
            )
            rejections.append(f"{rel}: model returned neither edit blocks nor a unified diff")
            if try_fallback(rel, content):
                continue
            continue
        validity = _validate_diff(diff, rel, full_content if full_content is not None else content)
        if not validity.get("valid"):
            _add_artifact(db, run.run_id, "diff_rejected", rel, content_json=validity)
            warnings = ", ".join(str(item) for item in validity.get("warnings") or [])
            rejections.append(f"{rel}: generated diff failed validation{f' ({warnings})' if warnings else ''}")
            if try_fallback(rel, content):
                continue
            continue
        check = _git(repo_path, ["apply", "--check"], input_text=diff, timeout=60)
        if check.returncode != 0:
            stderr = _clip(check.stderr or check.stdout or "", ERROR_SNIPPET_LIMIT)
            _add_artifact(
                db,
                run.run_id,
                "diff_rejected",
                rel,
                content_json={
                    "reason": "git_apply_check_failed",
                    "stderr": stderr,
                },
            )
            rejections.append(f"{rel}: generated patch did not apply cleanly ({stderr.strip() or 'git apply --check failed'})")
            if try_fallback(rel, content):
                continue
            continue
        diffs.append(diff)
        _add_artifact(db, run.run_id, "diff", rel, content=diff)
    if not diffs and rejections:
        raise AutonomyBlocked("No usable implementation diffs were produced. " + " ".join(rejections[:3]))
    return diffs


def _deterministic_small_desktop_diff(rel: str, content: str, prompt: str) -> str | None:
    prompt_lower = (prompt or "").lower()
    if not _is_vague_small_request(prompt) or not _has_any_token(prompt_lower, DESKTOP_UI_PROMPT_TOKENS):
        return None
    if rel == DESKTOP_AUTOPILOT_PRESENTER_FILE:
        if "final fileItems = _mapList(plan['files']);" in content:
            return None
        if PRESENTER_PLAN_BODY_OLD_SNIPPET not in content:
            return None
        updated = content.replace(PRESENTER_PLAN_BODY_OLD_SNIPPET, PRESENTER_PLAN_BODY_NEW_SNIPPET, 1)
        return _unified_diff(rel, content, updated)
    if rel == DESKTOP_API_CLIENT_FILE:
        updated = content
        pattern = re.compile(
            r"(?m)^(?P<indent>\s*)final err = decoded\?\['error'\] \?\? "
            r"decoded\?\['detail'\] \?\? response\.body;"
        )
        if not pattern.search(updated):
            return None
        updated = pattern.sub(
            lambda match: (
                f"{match.group('indent')}final err = decoded?['error'] ?? decoded?['detail'] ?? "
                "userMessageForHttpStatus(response.statusCode);"
            ),
            updated,
            count=1,
        )
        return _unified_diff(rel, content, updated)
    if rel != DESKTOP_NETWORK_ERROR_FILE:
        return None
    updated = content
    if "case 403:" not in updated and "Access denied (403)" not in updated:
        anchor = "    case 502:\n"
        insert = (
            "    case 401:\n"
            "      return 'Authentication failed (401). Pair this desktop app again or check the Backend URL in Settings.';\n"
            "    case 403:\n"
            "      return 'Access denied (403). Pair this desktop app again, or check that Settings points at your local CHILI backend.';\n"
        )
        if anchor not in updated:
            return None
        updated = updated.replace(anchor, insert + anchor, 1)
    elif "HandshakeException" not in updated:
        anchor = "  if (s.contains('Connection refused')) {\n"
        insert = (
            "  if (s.contains('HandshakeException') || s.contains('CERTIFICATE_VERIFY_FAILED')) {\n"
            "    return 'Secure connection failed. Check that the Backend URL uses the right http/https scheme and that any local certificate is trusted.';\n"
            "  }\n"
        )
        if anchor not in updated:
            return None
        updated = updated.replace(anchor, insert + anchor, 1)
    elif "FormatException" not in updated:
        anchor = "  return s;\n"
        insert = (
            "  if (s.contains('FormatException') || s.contains('Unexpected character')) {\n"
            "    return 'The server sent a response this app could not read. Check that the Backend URL points at the CHILI API, not a login page or proxy error page.';\n"
            "  }\n"
        )
        if anchor not in updated:
            return None
        updated = updated.replace(anchor, insert + anchor, 1)
    else:
        return None
    return _unified_diff(rel, content, updated)


def _unified_diff(rel: str, before: str, after: str) -> str:
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    diff = difflib.unified_diff(
        before_lines,
        after_lines,
        fromfile=f"a/{rel}",
        tofile=f"b/{rel}",
        lineterm="",
    )
    return "\n".join(diff) + "\n"


def _extract_diff(text: str) -> str | None:
    raw = (text or "").strip()
    m = re.search(r"```(?:diff)?\s*\n(.*?)\n```", raw, re.DOTALL)
    if m:
        raw = m.group(1).strip()
    if "--- " not in raw or "+++ " not in raw:
        return None
    return raw + ("\n" if not raw.endswith("\n") else "")


def _git(cwd: Path, args: list[str], *, input_text: str | None = None, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    if input_text is None:
        return subprocess.run(
            ["git"] + args,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=timeout,
            env=subprocess_safe_env(),
        )
    proc = subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        input=input_text.encode("utf-8"),
        capture_output=True,
        timeout=timeout,
        env=subprocess_safe_env(),
    )
    return subprocess.CompletedProcess(
        proc.args,
        proc.returncode,
        stdout=proc.stdout.decode("utf-8", errors="replace"),
        stderr=proc.stderr.decode("utf-8", errors="replace"),
    )


def _git_text(cwd: Path, args: list[str], *, timeout: int = 120) -> str:
    proc = _git(cwd, args, timeout=timeout)
    if proc.returncode != 0:
        raise AutonomyBlocked((proc.stderr or proc.stdout or "git command failed").strip()[:600])
    return (proc.stdout or "").strip()


def _ensure_git_repo(path: Path) -> None:
    if _git(path, ["rev-parse", "--is-inside-work-tree"]).returncode != 0:
        raise AutonomyBlocked("Selected repo is not a git worktree.")


def integration_branch_name(run_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(run_id or "").strip()).strip(".-")
    if not safe:
        safe = uuid.uuid4().hex[:14]
    return f"project-auto-{safe}"


def _create_run_worktree(repo_path: Path, run: ProjectAutonomyRun, base_sha: str) -> tuple[str, Path]:
    base = _local_worktree_root() / "chili-project-autopilot"
    base.mkdir(parents=True, exist_ok=True)
    worktree = base / run.run_id
    branch = integration_branch_name(run.run_id)

    if worktree.exists():
        _git(repo_path, ["worktree", "remove", "--force", str(worktree)], timeout=120)
        shutil.rmtree(worktree, ignore_errors=True)
    _git(repo_path, ["worktree", "unlock", str(worktree)], timeout=30)
    _git(repo_path, ["worktree", "prune"], timeout=60)
    proc = _git(repo_path, ["worktree", "add", "-B", branch, str(worktree), base_sha], timeout=WORKTREE_GIT_TIMEOUT_SEC)
    if proc.returncode != 0:
        raise AutonomyBlocked(
            f"Could not create isolated worktree: {(proc.stderr or proc.stdout or '').strip()[:ERROR_SNIPPET_LIMIT]}"
        )
    if not worktree.is_dir():
        raise AutonomyBlocked(f"Could not create isolated worktree at {worktree}.")
    return branch, worktree


def _apply_diffs(worktree: Path, diffs: list[str]) -> None:
    if not diffs:
        raise AutonomyBlocked("No implementation diffs were generated.")
    patch = "\n".join(diff.rstrip() for diff in diffs) + "\n"
    check = _git(worktree, ["apply", "--check"], input_text=patch, timeout=120)
    if check.returncode != 0:
        raise AutonomyBlocked(
            f"Generated diff did not apply cleanly: {(check.stderr or check.stdout or '').strip()[:ERROR_SNIPPET_LIMIT]}"
        )
    applied = _git(worktree, ["apply"], input_text=patch, timeout=120)
    if applied.returncode != 0:
        raise AutonomyBlocked(
            f"Could not apply generated diff: {(applied.stderr or applied.stdout or '').strip()[:ERROR_SNIPPET_LIMIT]}"
        )


def _changed_files(worktree: Path) -> list[str]:
    proc = _git(worktree, ["diff", "--name-only"], timeout=60)
    files = []
    for line in (proc.stdout or "").splitlines():
        rel = _safe_rel_path(line)
        if rel:
            files.append(rel)
    return sorted(dict.fromkeys(files))


def _commit_if_needed(worktree: Path, run: ProjectAutonomyRun) -> str | None:
    _git(worktree, ["add", "-A"], timeout=120)
    quiet = _git(worktree, ["diff", "--cached", "--quiet"], timeout=60)
    if quiet.returncode == 0:
        return None
    message = (
        f"[project-autopilot] {run.prompt[:90].replace(chr(10), ' ').strip() or run.run_id}\n\n"
        f"Generated by Project Brain Local Autopilot run {run.run_id}."
    )
    proc = _git(
        worktree,
        [
            "-c",
            "user.name=CHILI Autopilot",
            "-c",
            "user.email=chili-autopilot@local",
            "commit",
            "-m",
            message,
        ],
        timeout=WORKTREE_GIT_TIMEOUT_SEC,
    )
    if proc.returncode != 0:
        raise AutonomyBlocked(
            f"Commit failed in integration branch: {(proc.stderr or proc.stdout or '').strip()[:ERROR_SNIPPET_LIMIT]}"
        )
    return _git_text(worktree, ["rev-parse", "HEAD"], timeout=60)


def command_allowed(argv: list[str], cwd: Path) -> tuple[bool, str | None]:
    if not argv:
        return False, "empty command"
    normalized = [str(part).strip() for part in argv if str(part).strip()]
    lowered = [part.lower() for part in normalized]
    if not normalized:
        return False, "empty command"
    dangerous = {"rm", "del", "erase", "rmdir", "format", "curl", "wget", "pip", "poetry", "uv", "pnpm", "yarn"}
    if lowered[0] in dangerous:
        return False, "installs, network, and destructive commands require escalation"
    if lowered[:3] == [sys.executable.lower(), "-m", "pytest"] or lowered[:2] == ["python", "-m"]:
        return True, None
    allowed_prefixes = (
        ("pytest",),
        ("ruff", "check"),
        ("mypy",),
        ("npm", "test"),
        ("npm", "run", "lint"),
        ("npm", "run", "test"),
        ("npm", "run", "build"),
        ("git", "status"),
        ("git", "diff"),
    )
    for prefix in allowed_prefixes:
        if tuple(lowered[: len(prefix)]) == prefix:
            return True, None
    if lowered[:2] == ["npm", "run"] and len(lowered) >= 3:
        scripts = _package_scripts(cwd)
        if lowered[2] in {"lint", "test", "build"} and lowered[2] in scripts:
            return True, None
    return False, "command is not in the Project Autopilot allowlist"


def _package_scripts(cwd: Path) -> set[str]:
    pkg = cwd / "package.json"
    if not pkg.is_file():
        return set()
    try:
        data = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return set()
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if not isinstance(scripts, dict):
        return set()
    return {str(k).lower() for k in scripts.keys()}


def _run_allowlisted(argv: list[str], cwd: Path, *, timeout: int = 300) -> StepResult:
    ok, reason = command_allowed(argv, cwd)
    key = "_".join(part.replace("-", "") for part in argv[:3])[:60] or "command"
    if not ok:
        return StepResult(key, 0, False, "", "", True, reason)
    env = subprocess_safe_env()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        return StepResult(key, 0, False, "", f"executable not found: {argv[0]}", True, f"{argv[0]} not available")
    try:
        out, err = proc.communicate(timeout=timeout)
        out_t = _clip(out)
        err_t = _clip(err)
        return StepResult(key, proc.returncode or 0, False, out_t, err_t, False, None)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        return StepResult(key, -1, True, _clip(out), _clip((err or "") + "\n[timeout]"), False, None)


def _step_result_payload(result: StepResult) -> dict[str, Any]:
    payload = {
        "step_key": result.step_key,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "skipped": result.skipped,
        "skip_reason": result.skip_reason,
        "passed": result.exit_code == 0,
    }
    metadata = getattr(result, "metadata", None)
    if isinstance(metadata, Mapping):
        payload.update(metadata)
    return payload


def run_validation(worktree: Path, changed_files: list[str]) -> list[dict[str, Any]]:
    results: list[StepResult] = [
        run_ast_syntax(worktree, changed_files=changed_files),
        run_ruff_check(worktree),
        run_pytest_targeted(worktree, changed_files),
        run_mypy_check(worktree),
    ]
    scripts = _package_scripts(worktree)
    npm = shutil.which("npm")
    if npm:
        for script in ("lint", "test", "build"):
            if script in scripts:
                results.append(_run_allowlisted(["npm", "run", script], worktree, timeout=300))
    else:
        for script in ("lint", "test", "build"):
            if script in scripts:
                results.append(StepResult(f"npm_run_{script}", 0, False, "", "", True, "npm not available"))
    return [_step_result_payload(result) for result in results]


def validation_passed(results: list[dict[str, Any]]) -> bool:
    return all(int(item.get("exit_code") or 0) == 0 for item in results)


def _plan_declared_paths(plan: Mapping[str, Any] | None) -> list[str]:
    files = plan.get("files") if isinstance(plan, Mapping) else []
    paths: list[str] = []
    if not isinstance(files, list):
        return paths
    for item in files:
        if not isinstance(item, Mapping):
            continue
        rel = _safe_rel_path(str(item.get("path") or ""))
        if rel:
            paths.append(rel)
    return sorted(dict.fromkeys(paths))


def change_blast_radius_gate(
    plan: Mapping[str, Any] | None,
    changed_files: Sequence[str] | Iterable[str],
) -> dict[str, Any]:
    planned = set(_plan_declared_paths(plan))
    changed = {
        rel
        for value in changed_files
        for rel in [_safe_rel_path(str(value))]
        if rel
    }
    if changed and not planned:
        return {
            "passed": False,
            "reason": "Patch changed files but the plan did not declare an editable file scope.",
            "planned_files": [],
            "changed_files": sorted(changed),
            "unplanned_files": sorted(changed),
        }
    unplanned = sorted(changed - planned)
    if unplanned:
        return {
            "passed": False,
            "reason": "Patch changed files outside the approved plan scope.",
            "planned_files": sorted(planned),
            "changed_files": sorted(changed),
            "unplanned_files": unplanned,
        }
    return {
        "passed": True,
        "reason": "Changed files are inside the approved plan scope.",
        "planned_files": sorted(planned),
        "changed_files": sorted(changed),
        "unplanned_files": [],
    }


def _parse_numstat(numstat_text: str | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw_line in (numstat_text or "").splitlines():
        parts = raw_line.split("\t")
        if len(parts) < 3:
            continue
        try:
            added = int(parts[0])
            deleted = int(parts[1])
        except ValueError:
            added = 0
            deleted = 0
        rel = _safe_rel_path(parts[2])
        if rel:
            rows.append({"path": rel, "added": added, "deleted": deleted, "total": added + deleted})
    return rows


def patch_self_review_gate(
    plan: Mapping[str, Any] | None,
    changed_files: Sequence[str] | Iterable[str],
    *,
    numstat_text: str | None = None,
    name_status_text: str | None = None,
) -> dict[str, Any]:
    rows = _parse_numstat(numstat_text)
    changed = {
        rel
        for value in changed_files
        for rel in [_safe_rel_path(str(value))]
        if rel
    }
    if name_status_text:
        for raw_line in name_status_text.splitlines():
            parts = raw_line.split("\t")
            rel = _safe_rel_path(parts[-1] if parts else "")
            if rel:
                changed.add(rel)
    planned = set(_plan_declared_paths(plan))
    total_delta = sum(int(row["total"]) for row in rows)
    largest_file_delta = max((int(row["total"]) for row in rows), default=0)
    file_limit = max(8, len(planned) * 3 if planned else 8)
    blockers: list[str] = []
    if len(changed) > file_limit:
        blockers.append(f"changed {len(changed)} files; limit is {file_limit}")
    if total_delta > 300:
        blockers.append(f"patch changed {total_delta} lines; limit is 300")
    if largest_file_delta > 250:
        blockers.append(f"largest file delta is {largest_file_delta} lines; limit is 250")
    if blockers:
        return {
            "passed": False,
            "reason": "Patch is too broad for automatic merge: " + "; ".join(blockers),
            "changed_files": sorted(changed),
            "total_delta": total_delta,
            "largest_file_delta": largest_file_delta,
            "blockers": blockers,
        }
    return {
        "passed": True,
        "reason": "Patch size is small enough for automatic review.",
        "changed_files": sorted(changed),
        "total_delta": total_delta,
        "largest_file_delta": largest_file_delta,
        "blockers": [],
    }


def _validation_step_failed(item: Mapping[str, Any]) -> bool:
    if item.get("skipped") is True:
        return False
    try:
        return int(item.get("exit_code") or 0) != 0
    except (TypeError, ValueError):
        return True


def _is_collect_only_validation(item: Mapping[str, Any]) -> bool:
    scope = str(item.get("validation_scope") or "").lower()
    command = str(item.get("command") or "").lower()
    return bool(item.get("fallback_collect_only")) or "collect_only" in scope or "--collect-only" in command


def _is_targeted_test_validation(item: Mapping[str, Any]) -> bool:
    if item.get("skipped") is True or _validation_step_failed(item) or _is_collect_only_validation(item):
        return False
    step_key = str(item.get("step_key") or "").lower()
    command = str(item.get("command") or "").lower()
    test_files = item.get("test_files")
    has_test_file = isinstance(test_files, list) and any(str(value).strip() for value in test_files)
    targeted = item.get("targeted") is True or "target" in str(item.get("validation_scope") or "").lower()
    return ("pytest" in step_key or "pytest" in command) and targeted and has_test_file


def _is_changed_file_syntax_validation(item: Mapping[str, Any]) -> bool:
    if item.get("skipped") is True or _validation_step_failed(item):
        return False
    step_key = str(item.get("step_key") or "").lower()
    changed = item.get("changed_files")
    return "syntax" in step_key and isinstance(changed, list) and bool(changed)


def validation_merge_evidence(
    validation: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    changed_files: Sequence[str] | Iterable[str],
) -> dict[str, Any]:
    items = [dict(item) for item in validation if isinstance(item, Mapping)]
    changed = sorted(
        rel
        for value in changed_files
        for rel in [_safe_rel_path(str(value))]
        if rel
    )
    failed = [item for item in items if _validation_step_failed(item)]
    if failed:
        return {
            "passed": False,
            "reason": "At least one validation step failed.",
            "failed_steps": [str(item.get("step_key") or "validation") for item in failed],
            "changed_files": changed,
            "evidence_classes": [],
        }
    evidence_classes: list[str] = []
    if any(_is_changed_file_syntax_validation(item) for item in items):
        evidence_classes.append("changed_file_syntax")
    if any(_is_targeted_test_validation(item) for item in items):
        evidence_classes.append("targeted_tests")
    if not evidence_classes:
        return {
            "passed": False,
            "reason": "Validation did not include changed-file syntax or targeted test evidence.",
            "failed_steps": [],
            "changed_files": changed,
            "evidence_classes": [],
        }
    return {
        "passed": True,
        "reason": "Validation includes merge-relevant evidence.",
        "failed_steps": [],
        "changed_files": changed,
        "evidence_classes": evidence_classes,
    }


def behavior_validation_evidence(
    validation: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    changed_files: Sequence[str] | Iterable[str],
) -> dict[str, Any]:
    items = [dict(item) for item in validation if isinstance(item, Mapping)]
    merge_gate = validation_merge_evidence(items, changed_files)
    if not merge_gate.get("passed"):
        return {
            "passed": False,
            "reason": str(merge_gate.get("reason") or "Validation evidence is not merge-ready."),
            "targeted_tests": [],
            "changed_files": merge_gate.get("changed_files") or [],
        }
    targeted = [item for item in items if _is_targeted_test_validation(item)]
    if not targeted:
        return {
            "passed": False,
            "reason": "Behavior-changing code needs targeted test evidence, not syntax-only validation.",
            "targeted_tests": [],
            "changed_files": merge_gate.get("changed_files") or [],
        }
    return {
        "passed": True,
        "reason": "Targeted behavior tests are present.",
        "targeted_tests": [str(test) for item in targeted for test in (item.get("test_files") or [])],
        "changed_files": merge_gate.get("changed_files") or [],
    }


_DOMAIN_INVARIANT_RULES: tuple[tuple[tuple[str, ...], str, tuple[str, ...]], ...] = (
    (("pdt_guard",), "pdt", ("pdt", "day-trade", "day trade", "intraday", "margin")),
    (("broker_position", "broker_truth"), "broker_truth", ("broker truth", "readback", "reconcile", "reconciliation")),
    (("order_watchdog", "order_state"), "order_state", ("order state", "state machine", "stuck order", "watchdog")),
    (("management_envelopes", "position_identity"), "position_identity", ("position identity", "linked trade", "management envelope")),
    (("portfolio_risk", "capital_risk"), "capital_risk", ("capital risk", "portfolio risk", "drawdown budget")),
    (("monthly_dd", "drawdown"), "drawdown", ("drawdown", "loss breaker", "stop bleed")),
    (("trading_scheduler", "live_control"), "live_control", ("live runtime", "runtime control", "live control", "breaker")),
)


def _required_domain_invariants(changed_files: Sequence[str] | Iterable[str]) -> list[dict[str, Any]]:
    required: list[dict[str, Any]] = []
    for value in changed_files:
        rel = _safe_rel_path(str(value))
        if not rel:
            continue
        lower = rel.lower()
        invariants = [
            invariant
            for tokens, invariant, _evidence_terms in _DOMAIN_INVARIANT_RULES
            if any(token in lower for token in tokens)
        ]
        if invariants:
            required.append({"source_file": rel, "invariants": sorted(dict.fromkeys(invariants))})
    return required


def _validation_haystack(item: Mapping[str, Any]) -> str:
    chunks: list[str] = [
        str(item.get("step_key") or ""),
        str(item.get("command") or ""),
        str(item.get("validation_scope") or ""),
    ]
    for test_file in item.get("test_files") or []:
        chunks.append(str(test_file))
    for selected in item.get("test_selection") or []:
        if isinstance(selected, Mapping):
            chunks.extend(str(selected.get(key) or "") for key in ("test_file", "reason"))
    return " ".join(chunks).lower().replace("_", " ").replace("-", " ")


def domain_behavior_validation_evidence(
    validation: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    changed_files: Sequence[str] | Iterable[str],
) -> dict[str, Any]:
    items = [dict(item) for item in validation if isinstance(item, Mapping)]
    required = _required_domain_invariants(changed_files)
    required_names = sorted(
        dict.fromkeys(
            invariant
            for item in required
            for invariant in item.get("invariants", [])
        )
    )
    covered_names: set[str] = set()
    for item in items:
        if not _is_targeted_test_validation(item):
            continue
        haystack = _validation_haystack(item)
        for _tokens, invariant, evidence_terms in _DOMAIN_INVARIANT_RULES:
            if invariant not in required_names:
                continue
            if any(term.replace("_", " ") in haystack for term in evidence_terms):
                covered_names.add(invariant)
    covered = [
        {"source_file": "validation", "invariants": [invariant]}
        for invariant in required_names
        if invariant in covered_names
    ]
    missing = [
        {"source_file": "validation", "invariants": [invariant]}
        for invariant in required_names
        if invariant not in covered_names
    ]
    behavior_gate = behavior_validation_evidence(items, changed_files)
    blockers: list[str] = []
    if not behavior_gate.get("passed"):
        blockers.append(str(behavior_gate.get("reason") or "Targeted behavior tests are missing."))
    if missing:
        blockers.append("Missing invariant evidence: " + ", ".join(item["invariants"][0] for item in missing))
    return {
        "passed": not blockers,
        "reason": "Domain behavior evidence is complete." if not blockers else "; ".join(blockers),
        "required_domain_invariants": required,
        "covered_domain_invariants": covered,
        "missing_invariant_evidence": missing,
        "blockers": blockers,
    }


def semantic_patch_review_gate(
    plan: Mapping[str, Any] | None,
    changed_files: Sequence[str] | Iterable[str],
    *,
    diff_text: str | None = None,
    validation: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    text = diff_text or ""
    changed = [rel for value in changed_files for rel in [_safe_rel_path(str(value))] if rel]
    public_contract_change = bool(
        re.search(r"^-def\s+[A-Za-z_]\w*\(", text, re.MULTILINE)
        or re.search(r"^-class\s+[A-Za-z_]\w*", text, re.MULTILINE)
        or any(path.startswith(("app/routers/", "app/api/")) for path in changed)
    )
    tests = [
        str(test)
        for item in validation
        if isinstance(item, Mapping)
        for test in (item.get("test_files") or [])
    ]
    has_contract_tests = any(
        "contract" in test.lower() or "api" in test.lower() or "router" in test.lower()
        for test in tests
    )
    if public_contract_change and not has_contract_tests:
        return {
            "passed": False,
            "reason": "Public contract changes need API or contract test evidence before merge.",
            "changed_files": changed,
            "public_contract_change": True,
            "contract_tests": tests,
        }
    return {
        "passed": True,
        "reason": "Semantic patch review found no untested public contract change.",
        "changed_files": changed,
        "public_contract_change": public_contract_change,
        "contract_tests": tests,
    }


def validation_repair_context(
    validation: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    *,
    changed_files: Sequence[str] | Iterable[str] = (),
    plan_files: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    failed_steps: list[dict[str, Any]] = []
    for item in validation:
        if not isinstance(item, Mapping) or not _validation_step_failed(item):
            continue
        failed_steps.append(
            {
                "step_key": str(item.get("step_key") or "validation"),
                "exit_code": item.get("exit_code"),
                "command": str(item.get("command") or ""),
                "test_files": [str(value) for value in (item.get("test_files") or [])],
                "test_selection": item.get("test_selection") if isinstance(item.get("test_selection"), list) else [],
                "stdout_tail": truncate_text(str(item.get("stdout") or ""), 1800),
                "stderr_tail": truncate_text(str(item.get("stderr") or ""), 1800),
            }
        )
    return {
        "schema": "chili.validation-repair-context.v1",
        "changed_files": [rel for value in changed_files for rel in [_safe_rel_path(str(value))] if rel],
        "plan_files": [dict(item) for item in plan_files if isinstance(item, Mapping)],
        "failed_steps": failed_steps,
    }


def validation_repair_context_text(context: Mapping[str, Any]) -> str:
    changed_text = ", ".join(str(value) for value in context.get("changed_files") or []) or "none"
    lines = [
        "schema: chili.validation-repair-context.v1",
        "changed_files: " + changed_text,
    ]
    for index, item in enumerate(context.get("failed_steps") or [], start=1):
        if not isinstance(item, Mapping):
            continue
        lines.append(f"failed_step[{index}]: {item.get('step_key')} exit={item.get('exit_code')}")
        if item.get("command"):
            lines.append(f"command: {item.get('command')}")
        tests = ", ".join(str(value) for value in item.get("test_files") or [])
        if tests:
            lines.append(f"tests: {tests}")
        stdout_tail = str(item.get("stdout_tail") or "").strip()
        stderr_tail = str(item.get("stderr_tail") or "").strip()
        if stdout_tail:
            lines.append("stdout_tail:\n" + stdout_tail)
        if stderr_tail:
            lines.append("stderr_tail:\n" + stderr_tail)
    return "\n".join(lines).strip()


def _run_needs_visual_qa(run: Any, plan: Mapping[str, Any] | None) -> bool:
    status = str(getattr(run, "status", "") or "").lower()
    if status not in {RUN_STATUS_COMPLETED, RUN_STATUS_MERGED, "completed", "merged"}:
        return False
    prompt = str(getattr(run, "prompt", "") or "").lower()
    visual_prompt = any(token in prompt for token in ("ui", "screen", "layout", "visible", "frontend", "button"))
    visual_paths = []
    for rel in _plan_declared_paths(plan):
        lower = rel.lower()
        suffix = Path(lower).suffix
        if suffix in {".dart", ".tsx", ".jsx", ".html", ".css"} and any(
            token in lower
            for token in ("screen", "view", "widget", "component", "mobile", "desktop", "src/brain")
        ):
            visual_paths.append(rel)
    return bool(visual_paths and (visual_prompt or "screen" in " ".join(visual_paths).lower()))


def _validation_failure_text(results: list[dict[str, Any]]) -> str:
    failed = [r for r in results if int(r.get("exit_code") or 0) != 0]
    return "\n\n".join(
        f"{r.get('step_key')} exit={r.get('exit_code')}\n{r.get('stdout') or ''}\n{r.get('stderr') or ''}"
        for r in failed[:4]
    )[:5000]


def _dirty_files(repo_path: Path) -> list[str]:
    proc = _git(repo_path, ["status", "--porcelain"], timeout=60)
    files: list[str] = []
    for line in (proc.stdout or "").splitlines():
        rel = _safe_rel_path(line[3:])
        if rel:
            files.append(rel)
    return sorted(dict.fromkeys(files))


def _intersects(a: Iterable[str], b: Iterable[str]) -> bool:
    aa = {_safe_rel_path(x) for x in a}
    bb = {_safe_rel_path(x) for x in b}
    aa.discard(None)
    bb.discard(None)
    return bool(aa & bb)


def _attempt_merge(db: Session, run: ProjectAutonomyRun, repo_path: Path, changed_files: list[str]) -> dict[str, Any]:
    if run.repo_id is None:
        raise AutonomyBlocked("Run has no repo id.")
    acquire_merge_lease(db, run, int(run.repo_id))
    frozen_hits = frozen_scope.diff_touches_frozen_scope(changed_files)
    if frozen_scope.is_blocked(frozen_hits) or frozen_scope.requires_review(frozen_hits):
        msg = "Frozen-scope gate requires manual review."
        run.merge_status = "blocked"
        run.merge_message = msg
        _add_artifact(
            db,
            run.run_id,
            "merge_gate",
            "frozen_scope",
            content_json=[hit.__dict__ for hit in frozen_hits],
            commit=False,
        )
        return {"ok": False, "reason": msg}

    current_branch = _git_text(repo_path, ["branch", "--show-current"], timeout=60)
    current_head = _git_text(repo_path, ["rev-parse", "HEAD"], timeout=60)
    if run.base_branch and current_branch != run.base_branch:
        msg = f"Target checkout is on {current_branch!r}, expected {run.base_branch!r}."
        run.merge_status = "blocked"
        run.merge_message = msg
        return {"ok": False, "reason": msg}
    if run.base_sha and current_head != run.base_sha:
        msg = "Target branch moved since the autonomy run started."
        run.merge_status = "blocked"
        run.merge_message = msg
        return {"ok": False, "reason": msg}
    dirty = _dirty_files(repo_path)
    if dirty and _intersects(dirty, changed_files):
        msg = "Target checkout has dirty changes touching the autopilot scope."
        run.merge_status = "blocked"
        run.merge_message = msg
        return {"ok": False, "reason": msg, "dirty_files": dirty}
    proc = _git(repo_path, ["merge", "--ff-only", str(run.integration_branch)], timeout=WORKTREE_GIT_TIMEOUT_SEC)
    if proc.returncode != 0:
        msg = f"Merge was not clean: {(proc.stderr or proc.stdout or '').strip()[:ERROR_SNIPPET_LIMIT]}"
        run.merge_status = "blocked"
        run.merge_message = msg
        return {"ok": False, "reason": msg}
    run.merge_status = "merged"
    run.merge_message = f"Merged {run.integration_branch} into {run.base_branch or current_branch}."
    return {"ok": True, "message": run.merge_message}


def _record_learning(
    db: Session,
    run: ProjectAutonomyRun,
    *,
    outcome: str,
    plan: dict[str, Any],
    validation: list[dict[str, Any]],
) -> dict[str, Any]:
    payload = {
        "evidence_gated": True,
        "fine_tune_candidate": outcome in {"merged", "blocked", "completed"} and bool(validation),
        "promotion_status": "pending_eval",
        "outcome": outcome,
        "branch": run.integration_branch,
        "validation_passed": validation_passed(validation) if validation else False,
    }
    run.learning_json = _json_text(payload)
    db.add(
        ProjectAutonomyLearningSample(
            run_id=run.run_id,
            repo_id=run.repo_id,
            sample_type="trajectory",
            prompt=run.prompt,
            outcome=outcome,
            payload_json=_json_text({"plan": plan, "validation": validation, "learning": payload}),
            promoted=False,
        )
    )
    _add_artifact(db, run.run_id, "learning", "trajectory_sample", content_json=payload, commit=False)
    db.flush()
    return payload


def _build_reviewed_plan(
    db: Session,
    run: ProjectAutonomyRun,
    repo: CodeRepo,
    repo_path: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    try:
        context = _gather_context(db, int(repo.id), run.prompt, user_id=run.user_id)
    except Exception as exc:
        context = {"repos": [], "insights": [], "hotspots": [], "relevant_files": []}
        _add_artifact(
            db,
            run.run_id,
            "architect_review",
            "repo_context_unavailable",
            content_json={"error": _clip(str(exc), ERROR_SNIPPET_LIMIT)},
            commit=False,
        )
    context["operator_request"] = run.prompt
    candidate: dict[str, Any] | None = None
    latest_plan: dict[str, Any] = {}
    latest_files: list[dict[str, Any]] = []
    latest_review: dict[str, Any] = {}

    for attempt in range(1, ARCHITECT_REVIEW_MAX_ATTEMPTS + 1):
        _check_cancel(db, run)
        plan = candidate or build_local_plan(db, run, repo, context=context, repo_path=repo_path)
        plan.setdefault("analysis", "")
        plan.setdefault("files", [])
        plan.setdefault("notes", "")
        files = _plan_files(plan)
        review = _review_architect_plan(
            plan=plan,
            files=files,
            context=context,
            repo_path=repo_path,
            prompt=run.prompt,
            attempt_index=attempt,
        )
        _record_architect_review(db, run, review)
        _record_step(
            db,
            run,
            STAGE_ARCHITECT_REVIEW,
            "Architect reviewed plan quality",
            status=(
                "completed"
                if review.get("status") == ARCHITECT_REVIEW_STATUS_PASSED
                else "blocked"
            ),
            detail={
                "attempt_index": attempt,
                "score": review.get("score"),
                "status": review.get("status"),
                "blocking_reason": review.get("blocking_reason"),
            },
            commit=False,
        )
        db.commit()
        latest_plan = plan
        latest_files = files
        latest_review = review
        if review.get("status") == ARCHITECT_REVIEW_STATUS_PASSED:
            return latest_plan, latest_files, latest_review
        candidate = _revise_plan_from_review(plan, review, context, repo_path, run.prompt)
        if candidate is None:
            candidate = plan

    latest_review["status"] = ARCHITECT_REVIEW_STATUS_NEEDS_CLARIFICATION
    latest_review["blocking_reason"] = latest_review.get("blocking_reason") or "Plan quality gate failed after local revisions."
    _record_architect_review(db, run, latest_review)
    return latest_plan, latest_files, latest_review


def _run_planning_phase(db: Session, run: ProjectAutonomyRun, repo: CodeRepo, repo_path: Path) -> dict[str, Any]:
    run.status = RUN_STATUS_RUNNING
    run.plan_status = PLAN_STATUS_DRAFTING if run.plan_status != PLAN_STATUS_REVISING else PLAN_STATUS_REVISING
    if run.started_at is None:
        run.started_at = _utcnow()
    db.commit()

    _record_step(db, run, STAGE_CLASSIFY, "Classifying request", detail={"prompt_preview": run.prompt[:240]})
    _check_cancel(db, run)
    if _looks_like_live_monitoring_prompt(run.prompt):
        raise AutonomyBlocked(
            "This looks like a live monitoring/debugging request rather than a repo-editing task. "
            "Project Autopilot only starts autonomous worktrees for implementation prompts; use the live "
            "operator/chat monitor for this request, or ask Autopilot for a specific code change."
        )
    _ensure_git_repo(repo_path)
    base_branch = _git_text(repo_path, ["branch", "--show-current"], timeout=60)
    base_sha = _git_text(repo_path, ["rev-parse", "HEAD"], timeout=60)
    run.base_branch = base_branch
    run.target_branch = base_branch
    run.base_sha = base_sha
    db.commit()

    _record_step(db, run, STAGE_REPO_SCAN, "Scanning repository context", detail={"repo": repo.name, "path": str(repo_path)})
    _check_cancel(db, run)
    _record_step(db, run, STAGE_PLAN, "Architect is drafting an implementation plan")
    plan, files, review = _build_reviewed_plan(db, run, repo, repo_path)
    run.plan_json = _json_text(plan)
    run.files_json = _json_text([item["path"] for item in files])
    _add_artifact(db, run.run_id, "plan", "architect_plan", content_json=plan, commit=False)

    agents = assign_agent_lanes(files)
    run.agents_json = _json_text(agents)
    _record_step(db, run, STAGE_ASSIGN_ROLES, "Architect assigned agent lanes", detail={"agents": agents}, commit=False)
    _record_message(
        db,
        run,
        "assistant",
        _plan_message(plan, files, agents, review),
        message_type="plan",
        metadata={"plan": plan, "files": files, "agents": agents, "architect_review": review},
        commit=False,
    )
    if review.get("status") != ARCHITECT_REVIEW_STATUS_PASSED:
        run.status = RUN_STATUS_AWAITING_CLARIFICATION
        run.current_stage = STAGE_ARCHITECT_REVIEW
        run.plan_status = PLAN_STATUS_AWAITING_CLARIFICATION
        run.updated_at = _utcnow()
        _record_message(
            db,
            run,
            "assistant",
            _clarification_message(review),
            message_type="clarification",
            metadata={"architect_review": review},
            commit=False,
        )
        db.commit()
        db.refresh(run)
        return run_payload(db, run, include_events=True)
    if not files:
        raise AutonomyBlocked("The plan did not identify concrete files to change.")
    if run.execution_mode == EXECUTION_MODE_FULL_AUTOPILOT:
        run.plan_status = PLAN_STATUS_APPROVED
        db.commit()
        return _run_implementation_phase(db, run, repo, repo_path)

    run.status = RUN_STATUS_AWAITING_APPROVAL
    run.current_stage = STAGE_PLAN
    run.plan_status = PLAN_STATUS_AWAITING_APPROVAL
    run.updated_at = _utcnow()
    db.commit()
    db.refresh(run)
    return run_payload(db, run, include_events=True)


def _run_implementation_phase(db: Session, run: ProjectAutonomyRun, repo: CodeRepo, repo_path: Path) -> dict[str, Any]:
    plan = _json_load(run.plan_json, {})
    validation: list[dict[str, Any]] = []
    files = _plan_files(plan)
    if not files:
        raise AutonomyBlocked("The approved plan does not identify concrete files to change.")
    _ensure_git_repo(repo_path)
    if not run.base_branch:
        run.base_branch = _git_text(repo_path, ["branch", "--show-current"], timeout=60)
        run.target_branch = run.base_branch
    if not run.base_sha:
        run.base_sha = _git_text(repo_path, ["rev-parse", "HEAD"], timeout=60)
    run.status = RUN_STATUS_RUNNING
    run.current_stage = STAGE_IMPLEMENT
    run.plan_status = PLAN_STATUS_APPROVED
    if run.started_at is None:
        run.started_at = _utcnow()
    db.commit()

    base_sha = str(run.base_sha)
    branch, worktree = _create_run_worktree(repo_path, run, base_sha)
    run.integration_branch = branch
    run.worktree_path = str(worktree)
    acquire_repo_lease(db, run, int(repo.id))
    db.commit()
    _add_artifact(db, run.run_id, "worktree", "integration_worktree", content_json={"branch": branch, "path": str(worktree)})

    agents = _json_load(run.agents_json, [])
    if not agents:
        agents = assign_agent_lanes(files)
        run.agents_json = _json_text(agents)
    acquire_agent_file_leases(db, run, int(repo.id), agents)
    db.commit()

    _record_step(db, run, "implement", "Generating local implementation diffs", detail={"files": [f["path"] for f in files]})
    _check_cancel(db, run)
    diffs = generate_diffs_from_plan(db, run, worktree, files)
    _apply_diffs(worktree, diffs)
    changed_files = _changed_files(worktree)
    run.files_json = _json_text(changed_files)
    _record_step(db, run, "integrate", "Integrated generated diffs in isolated worktree", detail={"files": changed_files})
    db.commit()

    run.status = "validating"
    _record_step(db, run, "validate", "Running allowlisted validation commands", detail={"files": changed_files})
    validation = run_validation(worktree, changed_files)
    run.validation_json = _json_text(validation)
    run.commands_json = _json_text([{"step_key": item.get("step_key"), "exit_code": item.get("exit_code")} for item in validation])
    _add_artifact(db, run.run_id, "validation", "validation_results", content_json=validation, commit=False)
    db.commit()

    if not validation_passed(validation):
        _record_step(db, run, "repair", "Validation failed; attempting one local repair pass", status="completed")
        repair_context = _validation_failure_text(validation)
        repair_diffs = generate_diffs_from_plan(db, run, worktree, files, validation_context=repair_context)
        if repair_diffs:
            _apply_diffs(worktree, repair_diffs)
            changed_files = _changed_files(worktree)
            validation = run_validation(worktree, changed_files)
            run.files_json = _json_text(changed_files)
            run.validation_json = _json_text(validation)
            _add_artifact(db, run.run_id, "validation", "repair_validation_results", content_json=validation, commit=False)
            db.commit()

    commit_sha = _commit_if_needed(worktree, run)
    _add_artifact(db, run.run_id, "commit", "integration_commit", content_json={"commit_sha": commit_sha, "branch": branch})

    _record_step(db, run, "learn", "Recording evidence-gated learning sample")
    _record_learning(
        db,
        run,
        outcome="validated" if validation_passed(validation) else "validation_failed",
        plan=plan,
        validation=validation,
    )
    db.commit()

    if not validation_passed(validation):
        return run_payload(
            db,
            _finish(
                db,
                run,
                status="blocked",
                stage="validate",
                title="Autopilot blocked by validation",
                error_message=_validation_failure_text(validation),
                merge_status="blocked",
                merge_message="Validation failed after repair.",
            ),
            include_events=True,
        )

    run.status = "merging"
    _record_step(db, run, "merge", "Checking merge gates")
    merge_result = _attempt_merge(db, run, repo_path, changed_files)
    if merge_result.get("ok"):
        _record_learning(db, run, outcome="merged", plan=plan, validation=validation)
        return run_payload(
            db,
            _finish(
                db,
                run,
                status="merged",
                stage="merge",
                title="Autopilot merged safely",
                merge_status="merged",
                merge_message=str(merge_result.get("message") or "Merged."),
            ),
            include_events=True,
        )
    _record_learning(db, run, outcome="blocked", plan=plan, validation=validation)
    return run_payload(
        db,
        _finish(
            db,
            run,
            status="blocked",
            stage="merge",
            title="Autopilot produced a validated branch",
            merge_status="blocked",
            merge_message=str(merge_result.get("reason") or "Merge gate blocked."),
        ),
        include_events=True,
    )


def run_autonomy_sync(db: Session, run_id: str, on_event: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    run = _get_run_row(db, run_id)
    if run is None:
        raise ValueError(f"Unknown autonomy run: {run_id}")
    repo = _repo_for_row(db, run)
    repo_path = resolve_repo_runtime_path(repo) if repo is not None else None
    try:
        if repo is None or repo_path is None:
            raise AutonomyBlocked("Selected repo is no longer reachable.")
        if run.plan_status == PLAN_STATUS_APPROVED and _json_load(run.plan_json, {}):
            return _run_implementation_phase(db, run, repo, repo_path)
        return _run_planning_phase(db, run, repo, repo_path)
    except AutonomyCancelled as exc:
        return run_payload(
            db,
            _finish(
                db,
                run,
                status="cancelled",
                stage=run.current_stage or "cancelled",
                title="Autopilot cancelled",
                error_message=str(exc),
                merge_status="cancelled",
                merge_message="Cancelled by operator.",
            ),
            include_events=True,
        )
    except AutonomyBlocked as exc:
        plan = _json_load(run.plan_json, {})
        validation = _json_load(run.validation_json, [])
        _record_learning(db, run, outcome="blocked", plan=plan, validation=validation)
        return run_payload(
            db,
            _finish(
                db,
                run,
                status="blocked",
                stage=run.current_stage or "blocked",
                title="Autopilot blocked",
                error_message=str(exc),
                merge_status="blocked",
                merge_message=str(exc),
            ),
            include_events=True,
        )
    except Exception as exc:
        return run_payload(
            db,
            _finish(
                db,
                run,
                status="failed",
                stage=run.current_stage or "failed",
                title="Autopilot failed",
                error_message=str(exc),
                merge_status="failed",
                merge_message=str(exc),
            ),
            include_events=True,
        )
    finally:
        try:
            release_run_leases(db, run.run_id)
            db.commit()
        except Exception:
            db.rollback()
