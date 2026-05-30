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
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import uuid
import difflib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlparse

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ...models import (
    ProjectAutonomyAgentProfile,
    ProjectAutonomyAgentSchedule,
    ProjectAutonomyArchitectReview,
    ProjectAutonomyArtifact,
    ProjectAutonomyDelegation,
    ProjectAutonomyLearningSample,
    ProjectAutonomyLease,
    ProjectAutonomyMessage,
    ProjectAutonomyOperatorQuestion,
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
AGENT_PROFILE_STATUS_ACTIVE = "active"
AGENT_PROFILE_STATUS_PAUSED = "paused"
AGENT_PROFILE_TIER_MACRO = "macro"
AGENT_PROFILE_TIER_MICRO = "micro"
AGENT_PROFILE_TIER_SPECIALIST = "specialist"
AGENT_PERMISSION_OBSERVE = "observe"
AGENT_PERMISSION_RESEARCH = "research"
AGENT_PERMISSION_PLAN = "plan"
AGENT_PERMISSION_WORKTREE = "worktree"
AGENT_PERMISSION_MERGE = "merge"
AGENT_SCHEDULE_STATUS_PAUSED = "paused"
AGENT_SCHEDULE_STATUS_ACTIVE = "active"
DELEGATION_STATUS_PLANNED = "planned"
DELEGATION_STATUS_COMPLETED = "completed"
OPERATOR_QUESTION_STATUS_PENDING = "pending"
OPERATOR_QUESTION_STATUS_ANSWERED = "answered"
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
STAGE_PLAN = "plan"
STAGE_QUEUED = RUN_STATUS_QUEUED
STAGE_REPO_SCAN = "repo_scan"
STAGE_ASSIGN_ROLES = "assign_roles"
STAGE_ARCHITECT_REVIEW = "architect_review"
STAGE_COORDINATE = "coordinate"
EXPERT_WORKFLOW_MODE_PM_LED = "pm_led"
EXPERT_WORKFLOW_CHILD_AUTONOMY_LEVEL = "expert_workflow_child"
PM_COORDINATOR_PROFILE_KEY = "product_pm"
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
ARCHITECT_REVIEW_BLOCKER_OPERATOR_CHOICE_REQUIRED = "operator_choice_required"
DESKTOP_AUTOPILOT_PRESENTER_FILE = "chili_mobile/lib/src/brain/autonomy_run_presenter.dart"
DESKTOP_AUTOPILOT_COCKPIT_FILE = "chili_mobile/lib/src/brain/brain_dispatch_screen.dart"
DESKTOP_NETWORK_ERROR_FILE = "chili_mobile/lib/src/network/network_error_message.dart"
DESKTOP_API_CLIENT_FILE = "chili_mobile/lib/src/network/chili_api_client.dart"
OPEN_ENDED_ENHANCEMENT_BLOCKING_REASON = (
    "The request asks CHILI to choose any small enhancement. Pick a concrete direction or describe the "
    "user-visible behavior before implementation."
)
ARCHIVE_REASON_OPERATOR_CLEAR = "operator_clear"
AUTONOMY_LEVEL_SCHEDULED_AGENT = "scheduled_agent"
CODEX_AUTOMATION_PROFILE_PREFIX = "codex_"
CODEX_AUTOMATION_SOURCE = "codex_automation"
CODEX_AUTOMATION_CONFIG_DIR = "automations"
CODEX_AUTOMATION_CONFIG_FILE = "automation.toml"
CODEX_AUTOMATION_CWDS_KEY = "cwds"
CODEX_AUTOMATION_PROFILE_KEY_LIMIT = 80
CODEX_AUTOMATION_PROMPT_LIMIT = 16000
CODEX_AUTOMATION_PROMPT_PREVIEW_LIMIT = 220
CODEX_AUTOMATION_CONTRACT_VALUE_LIMIT = 220
CODEX_AUTOMATION_CONTRACT_COMMAND_LIMIT = 5
CODEX_AUTOMATION_CONTRACT_DECLARED_PATH_LIMIT = 3
CODEX_AUTOMATION_D_DRIVE_ROOT = r"D:\dev\chili-home-copilot"
CODEX_AUTOMATION_CONTRACT_LABELS = {
    "workspace": "Workspace",
    "inbox": "Inbox",
    "output": "Output",
    "state": "State",
}
CODEX_AUTOMATION_CONTRACT_COMMAND_PREFIXES = (
    "powershell",
    "python",
    "pytest",
    "flutter",
    "git ",
    "gh ",
    ".\\scripts\\",
    "scripts\\",
)
CODEX_AUTOMATION_CONTRACT_SAFETY_ACTIONS = (
    ("merge", "merge"),
    ("release", "release"),
    ("deploy", "deploy"),
    ("restart services", "restart services"),
    ("run migrations", "run migrations"),
    ("broker", "broker actions"),
    ("breaker", "breaker reset"),
    ("capital", "capital changes"),
    ("promote models", "model promotion"),
    ("live-trading", "live trading"),
    ("live trading", "live trading"),
)
CODEX_AUTOMATION_MIN_REPO_NAME_MATCH = 5
CODEX_AUTOMATION_REPO_ALIAS_TOKENS = {
    "chili_home_copilot": ("chili", "home copilot"),
}
CODEX_AUTOMATION_HASH_ALGORITHM = "sha256"
CODEX_AUTOMATION_PROMPT_HASH_KEY = "prompt_sha256"
CODEX_AUTOMATION_SYNC_STATUS_CURRENT = "current"
CODEX_AUTOMATION_SYNC_STATUS_STALE = "stale"
CODEX_AUTOMATION_SYNC_STATUS_CUSTOM = "custom_override"
CODEX_AUTOMATION_SYNC_STATUS_MISSING_SOURCE = "missing_source"
CODEX_AUTOMATION_SYNC_STATUS_MISSING_PROFILE = "missing_profile"
CODEX_AUTOMATION_SYNC_STATUS_NOT_CODEX = "not_codex"
CODEX_AUTOMATION_SYNC_REASON_PROMPT = "prompt_changed"
CODEX_AUTOMATION_SYNC_REASON_SCHEDULE = "schedule_changed"
CODEX_AUTOMATION_SYNC_REASON_STATUS = "status_changed"
CODEX_AUTOMATION_SYNC_REASON_MISSING_HASH = "missing_prompt_hash"
CODEX_AUTOMATION_OPERATOR_MODIFIED_KEY = "operator_modified"
CODEX_AUTOMATION_CADENCE_MANUAL = "manual"
CODEX_AUTOMATION_CADENCE_TWO_MINUTES = "two_minutes"
CODEX_AUTOMATION_CADENCE_FIVE_MINUTES = "five_minutes"
CODEX_AUTOMATION_CADENCE_TEN_MINUTES = "ten_minutes"
CODEX_AUTOMATION_CADENCE_HOURLY = "hourly"
CODEX_AUTOMATION_CADENCE_ALWAYS_ON = "always_on"
CODEX_AUTOMATION_RRULE_TWO_MINUTES = "FREQ=MINUTELY;INTERVAL=2"
CODEX_AUTOMATION_RRULE_FIVE_MINUTES = "FREQ=MINUTELY;INTERVAL=5"
CODEX_AUTOMATION_RRULE_TEN_MINUTES = "FREQ=MINUTELY;INTERVAL=10"
CODEX_AUTOMATION_RRULE_HOURLY = "FREQ=HOURLY;INTERVAL=1"
AGENT_SCHEDULE_RRULE_SEPARATOR = ";"
AGENT_SCHEDULE_RRULE_ASSIGN = "="
AGENT_SCHEDULE_RRULE_PREFIX = "RRULE:"
AGENT_SCHEDULE_RRULE_FREQ_KEY = "FREQ"
AGENT_SCHEDULE_RRULE_INTERVAL_KEY = "INTERVAL"
AGENT_SCHEDULE_RRULE_FREQ_MINUTELY = "MINUTELY"
AGENT_SCHEDULE_RRULE_FREQ_HOURLY = "HOURLY"
AGENT_SCHEDULE_DEFAULT_INTERVAL = 1
AGENT_SCHEDULE_MIN_INTERVAL = 1
AGENT_SCHEDULE_MAX_INTERVAL_MINUTES = 24 * 60
AGENT_SCHEDULE_MAX_INTERVAL_HOURS = 24
AGENT_SCHEDULE_DUE_CYCLE_LIMIT = 3
AGENT_SCHEDULE_SKIP_OPEN_CYCLE = "open_scheduled_cycle"
AGENT_SCHEDULE_SKIP_REPO_ALIAS = "non_preferred_repo_alias"
AGENT_SCHEDULE_SKIP_RUNTIME_REST = "runtime_rest"
AGENT_RUNTIME_MODE_KEY = "runtime_mode"
AGENT_RUNTIME_MODE_SCHEDULED = "scheduled"
AGENT_RUNTIME_MODE_ALWAYS_ON = "always_on"
AGENT_RUNTIME_WORK_STARTED_AT_KEY = "work_started_at"
AGENT_RUNTIME_REST_UNTIL_KEY = "rest_until"
AGENT_RUNTIME_WORK_WINDOW_MINUTES_KEY = "work_window_minutes"
AGENT_RUNTIME_REST_MINUTES_KEY = "rest_minutes"
AGENT_RUNTIME_DEFAULT_WORK_WINDOW_MINUTES = 4 * 60
AGENT_RUNTIME_DEFAULT_REST_MINUTES = 5
SCHEDULED_AGENT_REPORT_ARTIFACT_TYPE = "agent_cycle_report"
SCHEDULED_AGENT_REPORT_ARTIFACT_NAME = "scheduled_agent_report"
SCHEDULED_AGENT_REPORT_QUALITY_ARTIFACT_TYPE = "quality_gate"
SCHEDULED_AGENT_REPORT_QUALITY_ARTIFACT_NAME = "scheduled_agent_report_quality"
SCHEDULED_AGENT_REPORT_QUALITY_PASSING_SCORE = 75
SCHEDULED_AGENT_REPORT_QUALITY_PASSED = "passed"
SCHEDULED_AGENT_REPORT_QUALITY_REPAIRED = "repaired"
SCHEDULED_AGENT_REPORT_QUALITY_LOW = "low_quality"
SCHEDULED_AGENT_REPORT_NO_CHANGE_MARKERS = (
    "no files were changed",
    "no files changed",
    "plan-only",
    "observe",
    "research",
    "summar",
)
SCHEDULED_AGENT_REPORT_FALSE_ACTION_MARKERS = (
    "i changed",
    "changed files",
    "edited files",
    "implemented",
    "committed",
    "merged",
    "ran tests",
    "validated",
    "deployed",
)
AUTOPILOT_SLASH_PREFIX = "/"
AUTOPILOT_COMMAND_HELP = "help"
AUTOPILOT_COMMAND_STATUS = "status"
AUTOPILOT_COMMAND_AGENTS = "agents"
AUTOPILOT_COMMAND_PLAN = "plan"
AUTOPILOT_COMMAND_APPROVE = "approve"
AUTOPILOT_COMMAND_CANCEL = "cancel"
AUTOPILOT_COMMAND_CLEAR = "clear"
AUTOPILOT_COMMAND_MODEL = "model"
AUTOPILOT_COMMAND_SCHEDULE = "schedule"
AUTOPILOT_COMMAND_QUESTIONS = "questions"
AUTOPILOT_COMMAND_DOCTOR = "doctor"
AUTOPILOT_COMMAND_QUALITY = "quality"
AUTOPILOT_COMMAND_REFERENCE = "reference"
AUTOPILOT_COMMAND_MESSAGE_TYPE = "command"
AUTOPILOT_COMMAND_CLEAR_ARCHIVE_REASON = "slash_clear"
AUTOPILOT_COMMAND_SCHEDULE_ON = "on"
AUTOPILOT_COMMAND_SCHEDULE_OFF = "off"
AUTOPILOT_COMMAND_SCHEDULE_RESUME = "resume"
AUTOPILOT_COMMAND_SCHEDULE_PAUSE = "pause"
AUTOPILOT_COMMAND_SCHEDULE_CODEX = "codex"
AUTOPILOT_COMMAND_SCHEDULE_CODEX_ACTIVE = "codex-active"
AUTOPILOT_COMMAND_SCHEDULE_CODEX_ALWAYS_ON = "codex-always-on"
AUTOPILOT_COMMAND_SCHEDULE_CODEX_ADOPT = "codex-adopt"
AUTOPILOT_COMMAND_SCHEDULE_CODEX_PAUSE = "codex-pause"
AUTOPILOT_COMMAND_SCHEDULE_DEFAULT_RRULE = CODEX_AUTOMATION_RRULE_TEN_MINUTES
AUTOPILOT_SLASH_COMMANDS = frozenset({
    AUTOPILOT_COMMAND_HELP,
    AUTOPILOT_COMMAND_STATUS,
    AUTOPILOT_COMMAND_AGENTS,
    AUTOPILOT_COMMAND_PLAN,
    AUTOPILOT_COMMAND_APPROVE,
    AUTOPILOT_COMMAND_CANCEL,
    AUTOPILOT_COMMAND_CLEAR,
    AUTOPILOT_COMMAND_MODEL,
    AUTOPILOT_COMMAND_SCHEDULE,
    AUTOPILOT_COMMAND_QUESTIONS,
    AUTOPILOT_COMMAND_DOCTOR,
    AUTOPILOT_COMMAND_QUALITY,
    AUTOPILOT_COMMAND_REFERENCE,
})
AUTOPILOT_SLASH_HELP_LINES = (
    "/help - show Autopilot chat commands.",
    "/status - summarize this agent run.",
    "/agents - list repo agents and their paused/running state.",
    "/plan - start approval-first planning from this chat.",
    "/approve - approve a passing plan and start implementation.",
    "/cancel - stop this run at a safe checkpoint.",
    "/clear - archive this chat instance without deleting audit data.",
    "/model - show this agent's model policy and prompt source.",
    "/schedule [on|off|resume|pause|codex|codex-active|codex-always-on|codex-adopt|codex-pause] - inspect or change schedule state.",
    "/questions - show pending operator questions.",
    "/doctor - audit Agent OS readiness, local model quality, schedules, and safety gates.",
    "/quality - explain the guardrails compensating for weaker local models.",
    "/reference <path> - clean-room scan a local reference folder before using any ideas.",
)
REFERENCE_INTAKE_STATUS_SAFE = "safe"
REFERENCE_INTAKE_STATUS_CAUTION = "caution"
REFERENCE_INTAKE_STATUS_TAINTED = "tainted"
REFERENCE_INTAKE_MANIFEST_NAMES = frozenset({"package.json", "pyproject.toml", "Cargo.toml", "go.mod"})
REFERENCE_INTAKE_TEXT_NAMES = frozenset({"README.md", "README", "LICENSE", "NOTICE", "COPYING"})
REFERENCE_INTAKE_MAX_DEPTH = 2
REFERENCE_INTAKE_MAX_FILE_BYTES = 64000
REFERENCE_INTAKE_PREVIEW_LIMIT = 4
REFERENCE_INTAKE_TAINT_MARKERS = (
    "leaked",
    "unlicensed",
    "not an official release",
    "@anthropic-ai/claude-code",
    "claude code source",
)
REFERENCE_INTAKE_CAUTION_LICENSES = frozenset({"", "unknown", "unlicensed", "proprietary"})
REFERENCE_INTAKE_SAFE_PACKAGE_SIGNALS = {
    "@modelcontextprotocol/sdk": "MCP connector surface for local tools and agent capabilities",
    "@xterm/xterm": "terminal session cockpit for command output",
    "proper-lockfile": "durable file/process locks for agent coordination",
    "diff": "structured diff review and patch visualization",
    "chokidar": "file-watch refresh for repo context",
    "ignore": "gitignore-aware project scanning",
    "zod": "schema-validated tool and message contracts",
    "opentelemetry": "traceable agent telemetry and latency evidence",
    "react": "chat-style cockpit rendering patterns",
    "next": "web console shell patterns",
}
AUTOPILOT_DOCTOR_TITLE = "Agent OS doctor"
AUTOPILOT_DOCTOR_READY_COPY = "ready"
AUTOPILOT_DOCTOR_NEEDS_ATTENTION_COPY = "needs attention"
AUTOPILOT_DOCTOR_NO_ACTION_COPY = "No immediate action needed."
AGENT_OS_READINESS_READY = "ready"
AGENT_OS_READINESS_NEEDS_ATTENTION = "needs_attention"
AGENT_OS_READINESS_CHECK_PASSED = "passed"
AGENT_OS_READINESS_CHECK_WARNING = "warning"
AGENT_OS_READINESS_CHECK_FAILED = "failed"
AGENT_OS_READINESS_BASE_CHECK_COUNT = 6
AGENT_OS_READINESS_CHECK_PROFILES = "profiles"
AGENT_OS_READINESS_CHECK_CODEX = "codex_automations"
AGENT_OS_READINESS_CHECK_CODEX_FRESHNESS = "codex_prompt_freshness"
AGENT_OS_READINESS_CHECK_CODEX_CONTRACTS = "codex_operating_contracts"
AGENT_OS_READINESS_CHECK_SAFE_DEFAULTS = "safe_defaults"
AGENT_OS_READINESS_CHECK_SCHEDULES = "schedules"
AGENT_OS_READINESS_CHECK_QUESTIONS = "operator_questions"
AGENT_OS_READINESS_CHECK_REPO = "repo"
AGENT_OS_READINESS_CHECK_LOCAL_MODEL = "local_model"
AGENT_OS_READINESS_CHECK_TEAMS = "teams"
AGENT_OS_READINESS_CHECK_QUALITY_GOVERNANCE = "quality_governance"
AGENT_OS_READINESS_CHECK_RUNTIME_QUEUE = "runtime_queue"
AGENT_OS_READINESS_CHECK_OPERATOR_INBOX = "operator_inbox"
AGENT_OS_READINESS_CHECK_CODEX_ALIGNMENT = "codex_alignment"
AGENT_OS_CAPABILITY_AUDIT_KEY = "agent_os_capability_audit"
AGENT_OS_CAPABILITY_REPO_RUNTIME = "repo_runtime"
AGENT_OS_CAPABILITY_AGENT_HIERARCHY = "agent_hierarchy"
AGENT_OS_CAPABILITY_CODEX_MIRROR = "codex_prompt_mirror"
AGENT_OS_CAPABILITY_OPERATING_CONTRACTS = "operating_contracts"
AGENT_OS_CAPABILITY_SAFE_DEFAULTS = "safe_defaults"
AGENT_OS_CAPABILITY_RUNTIME_QUEUE = "runtime_queue"
AGENT_OS_CAPABILITY_ALWAYS_ON = "always_on_queue"
AGENT_OS_CAPABILITY_ARCHITECT_QUALITY = "architect_quality"
AGENT_OS_CAPABILITY_LOCAL_MODEL = "local_model_bridge"
AGENT_OS_CAPABILITY_OPERATOR_LOOP = "operator_loop"
AGENT_OS_CAPABILITY_VALIDATION_SAFETY = "validation_safety"
AGENT_OS_CAPABILITY_ACTION_KEEP_MONITORING = "keep_monitoring"
AGENT_OS_CAPABILITY_ACTION_FIX_REPO = "fix_repo_runtime"
AGENT_OS_CAPABILITY_ACTION_BOOTSTRAP = "bootstrap_agents"
AGENT_OS_CAPABILITY_ACTION_SYNC_CODEX = "sync_codex_prompts"
AGENT_OS_CAPABILITY_ACTION_REVIEW_CONTRACTS = "review_operating_contracts"
AGENT_OS_CAPABILITY_ACTION_REVIEW_PERMISSIONS = "review_permissions"
AGENT_OS_CAPABILITY_ACTION_DRAIN_QUEUE = "drain_runtime_queue"
AGENT_OS_CAPABILITY_ACTION_ENABLE_ALWAYS_ON = "enable_always_on"
AGENT_OS_CAPABILITY_ACTION_REVIEW_QUALITY = "review_quality"
AGENT_OS_CAPABILITY_ACTION_INSTALL_MODEL = "install_coder_model"
AGENT_OS_CAPABILITY_ACTION_ANSWER_OPERATOR = "answer_operator"
AGENT_OS_CAPABILITY_ACTION_RUN_VALIDATION = "run_validation"
AGENT_OS_LOCAL_MODEL_RECOMMENDATION = "Pull a local coder model, for example: ollama pull qwen2.5-coder:7b"
AGENT_OS_QUALITY_WINDOW_DAYS = 7
AGENT_OS_QUALITY_RECENT_RUN_LIMIT = 25
AGENT_OS_QUALITY_RECENT_ARTIFACT_LIMIT = 100
AGENT_OS_QUALITY_TERMINAL_MIN_SAMPLE = 3
AGENT_OS_QUALITY_BLOCKED_WARNING_RATIO = 0.5
AGENT_OS_QUALITY_RECENT_REVIEW_PREVIEW_LIMIT = 5
AGENT_OS_QUALITY_PROBLEM_PREVIEW_LIMIT = 3
AGENT_RUNTIME_QUEUE_RECENT_LIMIT = 80
AGENT_RUNTIME_QUEUE_WARNING_DEPTH = 5
AGENT_RUNTIME_QUEUE_PREVIEW_LIMIT = 5
AGENT_RUNTIME_QUEUE_STALE_ACTIVE_MINUTES = 30
AGENT_RUNTIME_QUEUE_ACTION_KEEP_MONITORING = "keep_monitoring"
AGENT_RUNTIME_QUEUE_ACTION_INSPECT_STALE = "inspect_stale_run"
AGENT_RUNTIME_QUEUE_ACTION_INSPECT_ACTIVE = "inspect_active_run"
AGENT_RUNTIME_QUEUE_ACTION_DRAIN_QUEUED = "drain_queued_run"
AGENT_RUNTIME_QUEUE_ACTION_REVIEW_WAITING = "review_waiting_run"
AGENT_OPERATOR_INBOX_RECENT_LIMIT = 80
AGENT_OPERATOR_INBOX_PREVIEW_LIMIT = 6
AGENT_OPERATOR_INBOX_ITEM_APPROVAL = "approval"
AGENT_OPERATOR_INBOX_ITEM_CLARIFICATION = "clarification"
AGENT_OPERATOR_INBOX_ITEM_QUESTION = "question"
AGENT_OPERATOR_INBOX_ITEM_BLOCKER = "blocker"
AGENT_OPERATOR_INBOX_ITEM_USER_REPLY = "user_reply"
AGENT_OPERATOR_INBOX_USER_ROLE = "user"
AGENT_OPERATOR_INBOX_ACTION_KEEP_MONITORING = "keep_monitoring"
AGENT_OPERATOR_INBOX_ACTION_ANSWER_QUESTION = "answer_operator_question"
AGENT_OPERATOR_INBOX_ACTION_ANSWER_CLARIFICATION = "answer_clarification"
AGENT_OPERATOR_INBOX_ACTION_REVIEW_APPROVAL = "review_approval"
AGENT_OPERATOR_INBOX_ACTION_CONTINUE_REPLY = "continue_reply"
AGENT_OPERATOR_INBOX_ACTION_REVIEW_BLOCKER = "review_blocker"
AGENT_OPERATING_STATE_NEEDS_INPUT = "needs_input"
AGENT_OPERATING_STATE_NEEDS_SYNC = "needs_sync"
AGENT_OPERATING_STATE_CUSTOM_PROMPT = "custom_prompt"
AGENT_OPERATING_STATE_RUNNING = "running"
AGENT_OPERATING_STATE_PAUSED_SOURCE_ACTIVE = "paused_source_active"
AGENT_OPERATING_STATE_PAUSED = "paused"
AGENT_OPERATING_STATE_MANUAL_READY = "manual_ready"
AGENT_OPERATING_STATE_SCHEDULED = "scheduled"
AGENT_OPERATING_STATE_READY = "ready"
AGENT_OPERATING_ACTION_ANSWER_QUESTION = "answer_operator_question"
AGENT_OPERATING_ACTION_SYNC_CODEX = "sync_codex_prompts"
AGENT_OPERATING_ACTION_REVIEW_CUSTOM = "review_custom_prompt"
AGENT_OPERATING_ACTION_OPEN_CHAT = "open_agent_chat"
AGENT_OPERATING_ACTION_ENABLE_ACTIVE = "enable_source_active_schedule"
AGENT_OPERATING_ACTION_RESUME = "resume_agent"
AGENT_OPERATING_ACTION_ENABLE_SCHEDULE = "enable_schedule"
AGENT_OPERATING_ACTION_WAIT_OR_RUN_NOW = "wait_or_run_now"
AGENT_OPERATING_ACTION_START_CHAT = "start_agent_chat"
AGENT_OPERATING_SAFETY_PLAN_ONLY = "plan_only"
AGENT_OPERATING_SAFETY_PATCH_CAPABLE = "patch_capable"
AGENT_OPERATING_SAFETY_MERGE_CAPABLE = "merge_capable"
AGENT_CODEX_ALIGNMENT_PASSING_SCORE = 85
AGENT_CODEX_ALIGNMENT_PASS_POINTS = 1.0
AGENT_CODEX_ALIGNMENT_WARNING_POINTS = 0.5
AGENT_CODEX_ALIGNMENT_FAIL_POINTS = 0.0
AGENT_CODEX_ALIGNMENT_DIMENSION_IMPORT = "imported_profiles"
AGENT_CODEX_ALIGNMENT_DIMENSION_FRESHNESS = "prompt_freshness"
AGENT_CODEX_ALIGNMENT_DIMENSION_CONTRACTS = "operating_contracts"
AGENT_CODEX_ALIGNMENT_DIMENSION_PROTOCOL = "operating_protocol"
AGENT_CODEX_ALIGNMENT_DIMENSION_SAFETY = "mutation_safety"
AGENT_CODEX_ALIGNMENT_DIMENSION_GOVERNANCE = "quality_governance"
AGENT_CODEX_ALIGNMENT_DIMENSION_RUNTIME = "runtime_flow"
AGENT_CODEX_ALIGNMENT_DIMENSION_MODEL = "local_model_bridge"
AGENT_CODEX_ALIGNMENT_PROTOCOL_COVERAGE_KEYS = (
    "mailbox_protocol_count",
    "run_lock_count",
    "out_report_count",
    "pr_review_flow_count",
)
AGENT_CODEX_BENCH_KEY = "codex_bench"
AGENT_CODEX_BENCH_ACTION_KEEP_MONITORING = "keep_monitoring"
AGENT_CODEX_BENCH_ACTION_SYNC = "sync_codex_prompts"
AGENT_CODEX_BENCH_ACTION_ENABLE_ACTIVE = "enable_source_active_schedule"
AGENT_CODEX_BENCH_ACTION_REVIEW_CUSTOM = "review_custom_prompts"
AGENT_CODEX_BENCH_ACTION_REVIEW_CONTRACTS = "review_operating_contracts"
AGENT_CODEX_BENCH_ACTION_REVIEW_ALIGNMENT = "review_codex_alignment"
AGENT_QUALITY_MONITOR_KEY = "agent_quality_monitor"
AGENT_QUALITY_MONITOR_DIMENSION_ARCHITECT = "architect_reviews"
AGENT_QUALITY_MONITOR_DIMENSION_SCHEDULED = "scheduled_reports"
AGENT_QUALITY_MONITOR_DIMENSION_VALIDATION = "validation"
AGENT_QUALITY_MONITOR_DIMENSION_MODEL = "local_model"
AGENT_QUALITY_MONITOR_DIMENSION_CODEX = "codex_alignment"
AGENT_QUALITY_MONITOR_DIMENSION_RUNTIME = "runtime_queue"
AGENT_QUALITY_MONITOR_DIMENSION_INBOX = "operator_inbox"
AGENT_QUALITY_MONITOR_ACTION_KEEP_MONITORING = "keep_monitoring"
AGENT_QUALITY_MONITOR_ACTION_INSTALL_MODEL = "install_coder_model"
AGENT_QUALITY_MONITOR_ACTION_ANSWER_INBOX = "answer_operator_inbox"
AGENT_QUALITY_MONITOR_ACTION_REVIEW_PLANS = "review_plan_gate"
AGENT_QUALITY_MONITOR_ACTION_REVIEW_REPORTS = "review_scheduled_reports"
AGENT_QUALITY_MONITOR_ACTION_RUN_VALIDATION = "run_validation"
AGENT_QUALITY_MONITOR_ACTION_SYNC_CODEX = "sync_codex_profiles"
AGENT_QUALITY_MONITOR_ACTION_DRAIN_QUEUE = "drain_runtime_queue"
AGENT_CODING_QUALITY_BAR_KEY = "agent_coding_quality_bar"
AGENT_CODING_QUALITY_BAR_TARGET_SCORE = 90
AGENT_CODING_QUALITY_BAR_DIMENSION_LOCAL_MODEL = "local_model_bridge"
AGENT_CODING_QUALITY_BAR_DIMENSION_QUALITY = "quality_governance"
AGENT_CODING_QUALITY_BAR_DIMENSION_CAPABILITY = "agent_os_capability"
AGENT_CODING_QUALITY_BAR_DIMENSION_CODEX = "codex_alignment"
AGENT_CODING_QUALITY_BAR_DIMENSION_RUNTIME = "runtime_control"
AGENT_CODING_QUALITY_BAR_DIMENSION_OPERATOR = "operator_recovery"
AGENT_CODING_QUALITY_BAR_ACTION_KEEP_MONITORING = "keep_monitoring"
AGENT_CODING_QUALITY_BAR_ACTION_INSTALL_MODEL = "install_coder_model"
AGENT_CODING_QUALITY_BAR_ACTION_REVIEW_QUALITY = "review_quality"
AGENT_CODING_QUALITY_BAR_ACTION_REVIEW_CAPABILITY = "review_agent_os_capability"
AGENT_CODING_QUALITY_BAR_ACTION_REVIEW_CODEX = "review_codex_alignment"
AGENT_CODING_QUALITY_BAR_ACTION_DRAIN_QUEUE = "drain_runtime_queue"
AGENT_CODING_QUALITY_BAR_ACTION_ANSWER_INBOX = "answer_operator_inbox"
DEFAULT_AGENT_PERMISSIONS = {
    AGENT_PERMISSION_OBSERVE: True,
    AGENT_PERMISSION_RESEARCH: True,
    AGENT_PERMISSION_PLAN: True,
    AGENT_PERMISSION_WORKTREE: False,
    AGENT_PERMISSION_MERGE: False,
}
DEFAULT_AGENT_SCHEDULE = {
    "enabled": False,
    "rrule": None,
    AGENT_RUNTIME_MODE_KEY: AGENT_RUNTIME_MODE_SCHEDULED,
    AGENT_RUNTIME_WORK_WINDOW_MINUTES_KEY: AGENT_RUNTIME_DEFAULT_WORK_WINDOW_MINUTES,
    AGENT_RUNTIME_REST_MINUTES_KEY: AGENT_RUNTIME_DEFAULT_REST_MINUTES,
    "budget": {"max_minutes": 20, "max_child_runs": 0},
}
LEGACY_FLAT_SUPERVISOR_KEYS = frozenset({PM_COORDINATOR_PROFILE_KEY, "architect"})
AGENT_DEFAULT_SUPERVISOR_RULES = (
    (
        "dba_architect",
        (
            "database",
            "postgres",
            "schema",
            "migration",
            "persistence",
            "query",
            "sql",
            "sdba",
            "db_quality",
            "db quality",
            "data platform",
            "mlops",
            "data scientist",
            "feature",
            "label",
        ),
    ),
    (
        "qa_manager",
        (
            "qa",
            "verification",
            "test",
            "quality",
            "security",
            "compliance",
            "validation",
        ),
    ),
    (
        "risk_reviewer",
        (
            "risk",
            "controls",
            "safety",
        ),
    ),
    (
        "algo_trading_architect",
        (
            "algo",
            "trading",
            "trader",
            "option",
            "coinbase",
            "alpha",
            "scalp",
            "broker",
            "portfolio",
            "market",
        ),
    ),
    (
        "dev_lead",
        (
            "frontend",
            "backend",
            "software",
            "engineer",
            "sswe",
            "devops",
            "sre",
            "performance",
            "cost",
            "ui",
            "brain",
            "ops",
            "flow",
            "hardening",
            "docs",
        ),
    ),
)
DEFAULT_AGENT_PROFILE_DEFINITIONS = (
    {
        "profile_key": "architect",
        "name": "Architect",
        "role": "dev_architect",
        "tier": AGENT_PROFILE_TIER_MACRO,
        "prompt": "Govern repo direction, plan safe architecture changes, and delegate only after review gates pass.",
    },
    {
        "profile_key": "dev_lead",
        "name": "Dev Lead",
        "role": "dev_lead",
        "tier": AGENT_PROFILE_TIER_MACRO,
        "prompt": "Coordinate implementation flow, file ownership, and repair loops for small engineering teams.",
    },
    {
        "profile_key": "qa_manager",
        "name": "QA Manager",
        "role": "qa_manager",
        "tier": AGENT_PROFILE_TIER_MACRO,
        "prompt": "Design validation strategy, decide test coverage, and request visual QA evidence when useful.",
    },
    {
        "profile_key": "dba_architect",
        "name": "DBA Architect",
        "role": "dba_architect",
        "tier": AGENT_PROFILE_TIER_MACRO,
        "prompt": "Review schema, migration, query, and persistence risks before database-affecting work.",
    },
    {
        "profile_key": "product_pm",
        "name": "Product PM",
        "role": "product_manager",
        "tier": AGENT_PROFILE_TIER_MACRO,
        "prompt": "Coordinate Project Autopilot requests, dispatch specialist experts, track dependencies, and keep implementation approval-first.",
    },
    {
        "profile_key": "software_engineer",
        "name": "Software Engineer",
        "role": "software_engineer",
        "tier": AGENT_PROFILE_TIER_MICRO,
        "prompt": "Plan scoped code changes, integration boundaries, and implementation handoffs without mutating files until approval.",
    },
    {
        "profile_key": "frontend",
        "name": "Frontend Engineer",
        "role": "frontend_engineer",
        "tier": AGENT_PROFILE_TIER_MICRO,
        "prompt": "Implement scoped UI changes with existing Flutter patterns and visual validation.",
    },
    {
        "profile_key": "backend",
        "name": "Backend Engineer",
        "role": "backend_engineer",
        "tier": AGENT_PROFILE_TIER_MICRO,
        "prompt": "Implement scoped backend/API changes with tests and migration safety.",
    },
    {
        "profile_key": "qa",
        "name": "QA Engineer",
        "role": "qa_engineer",
        "tier": AGENT_PROFILE_TIER_MICRO,
        "prompt": "Write and run focused validation, reproduce failures, and summarize residual risk.",
    },
    {
        "profile_key": "db_quality",
        "name": "DB Quality Engineer",
        "role": "db_quality_engineer",
        "tier": AGENT_PROFILE_TIER_MICRO,
        "prompt": "Inspect persistence, migrations, fixtures, and data-safety concerns.",
    },
    {
        "profile_key": "security",
        "name": "Security Engineer",
        "role": "security_engineer",
        "tier": AGENT_PROFILE_TIER_MICRO,
        "prompt": "Review permissions, paths, secrets, command policy, and unsafe operations.",
    },
    {
        "profile_key": "docs",
        "name": "Docs Engineer",
        "role": "docs_engineer",
        "tier": AGENT_PROFILE_TIER_MICRO,
        "prompt": "Keep operator-facing notes, acceptance criteria, and run summaries clear.",
    },
    {
        "profile_key": "data_scientist",
        "name": "Data Scientist",
        "role": "data_scientist",
        "tier": AGENT_PROFILE_TIER_SPECIALIST,
        "prompt": "Review data quality, metrics, evaluation evidence, and queue recommendations before project changes depend on them.",
    },
    {
        "profile_key": "risk_reviewer",
        "name": "Risk Reviewer",
        "role": "risk_reviewer",
        "tier": AGENT_PROFILE_TIER_SPECIALIST,
        "prompt": "Review project-level safety, live-trading risk, destructive-operation risk, and release gates before approval.",
    },
    {
        "profile_key": "sre",
        "name": "SRE",
        "role": "sre",
        "tier": AGENT_PROFILE_TIER_SPECIALIST,
        "prompt": "Review operations, deployment, observability, rollback, and runtime safety concerns.",
    },
    {
        "profile_key": "mlops",
        "name": "MLOps Engineer",
        "role": "mlops_engineer",
        "tier": AGENT_PROFILE_TIER_SPECIALIST,
        "prompt": "Review model lifecycle, evaluation, promotion, reproducibility, and monitoring concerns.",
    },
)
SPECIALIST_AGENT_PROFILE_DEFINITIONS = (
    {
        "profile_key": "algo_trading_architect",
        "name": "Algo Trading Architect",
        "role": "algo_trading_architect",
        "tier": AGENT_PROFILE_TIER_SPECIALIST,
        "prompt": "Advise on trading-system architecture, safety gates, evidence, and market-domain edge cases.",
        "tokens": ("trading", "strategy", "broker", "portfolio", "pattern", "market"),
    },
)
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
DESKTOP_AUTOPILOT_INPUT_BEHAVIOR_TOKENS = frozenset({
    "enter key",
    "enter sends",
    "enter to send",
    "message box",
    "prompt box",
    "send button",
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
OPEN_ENDED_ENHANCEMENT_OPTIONS = (
    {
        "path": DESKTOP_AUTOPILOT_COCKPIT_FILE,
        "reason": "Autopilot chat UX, composer controls, and approval actions live in the cockpit screen.",
    },
    {
        "path": DESKTOP_AUTOPILOT_PRESENTER_FILE,
        "reason": "Autopilot chat, plan, artifact, and validation prose is formatted by the presenter.",
    },
    {
        "path": "app/services/project_autonomy/orchestrator.py",
        "reason": "Plan quality gates, local-model fallbacks, safety states, and run recovery live in the orchestrator.",
    },
)
OPEN_ENDED_OPTION_LIMIT = 3
OPEN_ENDED_OPTION_SELECTION_PHRASES = {
    1: ("1", "one", "first", "first one", "option 1", "option one", "the first"),
    2: ("2", "two", "second", "second one", "option 2", "option two", "the second"),
    3: ("3", "three", "third", "third one", "option 3", "option three", "the third"),
}
PLAN_APPROVAL_NEGATION_MARKERS = frozenset({
    "do not approve",
    "do not implement",
    "don't approve",
    "don't implement",
    "dont approve",
    "dont implement",
    "not approve",
    "not approved",
    "not implement",
    "not yet",
    "hold off",
    "wait",
})
PLAN_APPROVAL_FEEDBACK_MARKERS = frozenset({
    "but",
    "except",
    "instead",
    "missing",
    "revise",
    "change",
    "modify",
    "add",
    "remove",
    "if",
})
PLAN_APPROVAL_PHRASES = frozenset({
    "approve",
    "approve it",
    "approve plan",
    "approve and implement",
    "approved",
    "do it",
    "go ahead",
    "implement it",
    "looks good",
    "proceed",
    "run it",
    "ship it",
    "start implementation",
    "yes approve",
    "yes implement",
})
RUN_CANCEL_EXACT_PHRASES = frozenset({
    "abort",
    "cancel",
    "never mind",
    "nevermind",
    "stop",
})
RUN_CANCEL_PHRASES = frozenset({
    "abort autopilot",
    "abort it",
    "abort run",
    "abort the run",
    "abort this run",
    "cancel autopilot",
    "cancel it",
    "cancel run",
    "cancel the run",
    "cancel this",
    "cancel this run",
    "stop autopilot",
    "stop it",
    "stop run",
    "stop the run",
    "stop this",
    "stop this run",
})
RUN_CANCEL_FEEDBACK_MARKERS = frozenset({
    "add",
    "button",
    "change",
    "copy",
    "fix",
    "implement",
    "label",
    "message",
    "remove",
    "revise",
    "should",
    "text",
})
RUN_CANCEL_INFORMATIONAL_MARKERS = frozenset({
    "how can",
    "how do",
    "what happens",
    "what should",
    "where can",
    "where do",
})
CONCRETE_FILE_REFERENCE_MARKERS = frozenset({
    "/",
    ".py",
    ".dart",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".html",
    ".css",
})
OPEN_ENDED_DIRECTION_TOKENS = frozenset({
    "approval",
    "artifact",
    "attachment",
    "chat",
    "composer",
    "dark mode",
    "enter key",
    "enter sends",
    "enter to send",
    "history",
    "image",
    "message box",
    "plan presentation",
    "prompt box",
    "right sidebar",
    "send button",
    "screenshot",
    "status",
    "timeline",
    "validation",
    "visual",
})
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


def _looks_like_plan_approval_message(message: str) -> bool:
    normalised = _normalised_choice_text(message)
    if not normalised:
        return False
    if any(_choice_text_contains_phrase(normalised, marker) for marker in PLAN_APPROVAL_NEGATION_MARKERS):
        return False
    if any(_choice_text_contains_phrase(normalised, marker) for marker in PLAN_APPROVAL_FEEDBACK_MARKERS):
        return False
    return any(_choice_text_contains_phrase(normalised, phrase) for phrase in PLAN_APPROVAL_PHRASES)


def _looks_like_run_cancel_message(message: str) -> bool:
    normalised = _normalised_choice_text(message)
    if not normalised:
        return False
    if any(_choice_text_contains_phrase(normalised, marker) for marker in RUN_CANCEL_INFORMATIONAL_MARKERS):
        return False
    if any(_choice_text_contains_phrase(normalised, marker) for marker in RUN_CANCEL_FEEDBACK_MARKERS):
        return False
    if normalised in RUN_CANCEL_EXACT_PHRASES:
        return True
    return any(_choice_text_contains_phrase(normalised, phrase) for phrase in RUN_CANCEL_PHRASES)


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
    mentions_input_behavior = _has_any_token(prompt_lower, DESKTOP_AUTOPILOT_INPUT_BEHAVIOR_TOKENS)
    has_autopilot_context = mentions_autopilot or _is_autopilot_chat_transcript(prompt_lower)
    return has_autopilot_context and mentions_composer and (mentions_attachment or mentions_input_behavior)


def _is_autopilot_chat_transcript(prompt_lower: str) -> bool:
    return "user message 1:" in prompt_lower and any(
        f"user message {index}:" in prompt_lower for index in range(2, 10)
    )


def _autopilot_cockpit_reason(prompt: str) -> str:
    prompt_lower = (prompt or "").lower()
    if _has_any_token(prompt_lower, DESKTOP_AUTOPILOT_ATTACHMENT_TOKENS):
        return "The request asks for Autopilot prompt attachments; those composer controls live in the desktop cockpit screen."
    if _has_any_token(prompt_lower, DESKTOP_AUTOPILOT_INPUT_BEHAVIOR_TOKENS):
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


def _requires_operator_choice_before_plan(prompt: str) -> bool:
    prompt_lower = (prompt or "").lower()
    if not _is_vague_small_request(prompt):
        return False
    if _is_autopilot_cockpit_request(prompt):
        return False
    if _selected_open_ended_option_index(prompt_lower) is not None:
        return False
    if _has_followup_operator_direction(prompt_lower):
        return False
    if _has_broad_desktop_plan_override(prompt_lower):
        return False
    if _has_any_token(prompt_lower, BROAD_DESKTOP_DETAIL_TOKENS):
        return False
    if _has_any_token(prompt_lower, CONCRETE_FILE_REFERENCE_MARKERS):
        return False
    return _has_any_token(prompt_lower, DESKTOP_UI_PROMPT_TOKENS | DESKTOP_AUTOPILOT_PROMPT_TOKENS)


def _has_followup_operator_direction(prompt_lower: str) -> bool:
    followups = _followup_user_messages(prompt_lower)
    if not followups:
        return False
    return any(_has_any_token(message, OPEN_ENDED_DIRECTION_TOKENS) for message in followups)


def _followup_user_messages(prompt_lower: str) -> list[str]:
    messages: list[str] = []
    for match in re.finditer(
        r"user message\s+(\d+):\s*(.*?)(?=\n\s*user message\s+\d+:|\Z)",
        prompt_lower,
        flags=re.DOTALL,
    ):
        try:
            message_index = int(match.group(1))
        except ValueError:
            continue
        if message_index >= 2:
            messages.append(match.group(2).strip())
    return messages


def _normalised_choice_text(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _choice_text_contains_phrase(normalised: str, phrase: str) -> bool:
    return re.search(rf"(?:^| ){re.escape(phrase)}(?: |$)", normalised) is not None


def _selected_open_ended_option_index(prompt_lower: str) -> int | None:
    for message in reversed(_followup_user_messages(prompt_lower)):
        normalised = _normalised_choice_text(message)
        if not normalised:
            continue
        for index, phrases in OPEN_ENDED_OPTION_SELECTION_PHRASES.items():
            if any(_choice_text_contains_phrase(normalised, phrase) for phrase in phrases):
                return index
    return None


def _selected_open_ended_option(prompt: str, repo_path: Path | None = None) -> dict[str, str] | None:
    index = _selected_open_ended_option_index((prompt or "").lower())
    if index is None or index < 1 or index > OPEN_ENDED_OPTION_LIMIT:
        return None
    available = [
        dict(option)
        for option in OPEN_ENDED_ENHANCEMENT_OPTIONS[:OPEN_ENDED_OPTION_LIMIT]
        if repo_path is None or _candidate_exists(repo_path, str(option.get("path") or ""))
    ]
    if index > len(available):
        return None
    selected = available[index - 1]
    path = str(selected.get("path") or "")
    reason = str(selected.get("reason") or "")
    if not path or not reason:
        return None
    return {"path": path, "reason": reason, "index": str(index)}


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


def _open_ended_option_plan(prompt: str, repo_path: Path | None) -> dict[str, Any] | None:
    selected = _selected_open_ended_option(prompt, repo_path)
    if selected is None:
        return None
    path = selected["path"]
    return {
        "analysis": (
            f"You chose option {selected['index']}. I will keep the enhancement narrow and tied to that "
            "clarification before asking for approval."
        ),
        "files": [
            {
                "path": path,
                "action": "modify",
                "description": selected["reason"],
            }
        ],
        "notes": "This plan is based on the operator's clarification selection and remains approval-first.",
    }


def _operator_safe_plan_payload(plan: dict[str, Any]) -> dict[str, Any]:
    if not plan:
        return {}
    safe = dict(plan)
    for key in ("analysis", "notes"):
        if key in safe:
            safe[key] = _operator_safe_plan_text(safe.get(key))
    return safe


def _run_payload(row: ProjectAutonomyRun) -> dict[str, Any]:
    plan = _json_load(row.plan_json, {})
    return {
        "id": row.id,
        "run_id": row.run_id,
        "project_run_id": row.project_run_id,
        "user_id": row.user_id,
        "repo_id": row.repo_id,
        "agent_profile_id": row.agent_profile_id,
        "parent_run_id": row.parent_run_id,
        "agent_snapshot": _json_load(row.agent_snapshot_json, {}),
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
        "architect_review": {},
        "agents": _json_load(row.agents_json, []),
        "files": _json_load(row.files_json, []),
        "commands": _json_load(row.commands_json, []),
        "validation": _json_load(row.validation_json, []),
        "learning": _json_load(row.learning_json, {}),
        "error_message": row.error_message,
        "cancel_requested": bool(row.cancel_requested),
        "archived": row.archived_at is not None,
        "archived_at": row.archived_at.isoformat() if row.archived_at else None,
        "archive_reason": row.archive_reason,
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


def _operator_question_payload(row: ProjectAutonomyOperatorQuestion) -> dict[str, Any]:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "agent_profile_id": row.agent_profile_id,
        "user_id": row.user_id,
        "repo_id": row.repo_id,
        "question": row.question,
        "context": _json_load(row.context_json, {}),
        "status": row.status,
        "answer": row.answer,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "answered_at": row.answered_at.isoformat() if row.answered_at else None,
    }


def _delegation_payload(row: ProjectAutonomyDelegation) -> dict[str, Any]:
    return {
        "id": row.id,
        "parent_run_id": row.parent_run_id,
        "child_run_id": row.child_run_id,
        "parent_agent_profile_id": row.parent_agent_profile_id,
        "child_agent_profile_id": row.child_agent_profile_id,
        "status": row.status,
        "intent": row.intent,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _latest_artifact_json(
    db: Session,
    run_id: str,
    *,
    artifact_type: str,
    name: str | None = None,
) -> dict[str, Any]:
    q = db.query(ProjectAutonomyArtifact).filter(
        ProjectAutonomyArtifact.run_id == run_id,
        ProjectAutonomyArtifact.artifact_type == artifact_type,
    )
    if name is not None:
        q = q.filter(ProjectAutonomyArtifact.name == name)
    artifact = q.order_by(ProjectAutonomyArtifact.id.desc()).first()
    if artifact is None:
        return {}
    payload = _json_load(artifact.content_json, {})
    return payload if isinstance(payload, dict) else {}


def _expert_threads_payload(
    db: Session,
    row: ProjectAutonomyRun,
    agents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not agents:
        return []
    child_ids = {
        str(item.get("child_run_id"))
        for item in agents
        if isinstance(item, dict) and item.get("child_run_id")
    }
    child_rows: dict[str, ProjectAutonomyRun] = {}
    if child_ids:
        child_rows = {
            child.run_id: child
            for child in (
                db.query(ProjectAutonomyRun)
                .filter(ProjectAutonomyRun.parent_run_id == row.run_id)
                .filter(ProjectAutonomyRun.run_id.in_(tuple(child_ids)))
                .all()
            )
        }
    out: list[dict[str, Any]] = []
    for item in agents:
        if not isinstance(item, dict):
            continue
        thread = dict(item)
        child_id = str(thread.get("child_run_id") or "")
        child = child_rows.get(child_id)
        if child is not None:
            child_profile = (
                db.get(ProjectAutonomyAgentProfile, int(child.agent_profile_id))
                if child.agent_profile_id is not None
                else None
            )
            thread["child_run"] = {
                "run_id": child.run_id,
                "status": child.status,
                "current_stage": child.current_stage,
                "plan_status": child.plan_status,
                "merge_status": child.merge_status,
                "chat_title": child.chat_title,
                "agent_profile_id": child.agent_profile_id,
                "agent_profile": (
                    _agent_profile_payload(db, child_profile)
                    if child_profile is not None
                    else {}
                ),
                "updated_at": child.updated_at.isoformat() if child.updated_at else None,
                "finished_at": child.finished_at.isoformat() if child.finished_at else None,
            }
            thread["status"] = str(thread.get("status") or child.status or "")
        out.append(thread)
    return out


def _parent_run_summary(db: Session, row: ProjectAutonomyRun) -> dict[str, Any]:
    parent_id = str(row.parent_run_id or "").strip()
    if not parent_id:
        return {}
    parent = (
        db.query(ProjectAutonomyRun)
        .filter(ProjectAutonomyRun.run_id == parent_id)
        .first()
    )
    if parent is None:
        return {"run_id": parent_id, "missing": True}
    parent_profile = (
        db.get(ProjectAutonomyAgentProfile, int(parent.agent_profile_id))
        if parent.agent_profile_id is not None
        else None
    )
    return {
        "run_id": parent.run_id,
        "status": parent.status,
        "current_stage": parent.current_stage,
        "plan_status": parent.plan_status,
        "merge_status": parent.merge_status,
        "chat_title": parent.chat_title,
        "prompt_preview": truncate_text(parent.prompt or "", 180)[0],
        "agent_profile_id": parent.agent_profile_id,
        "agent_profile": (
            _agent_profile_payload(db, parent_profile)
            if parent_profile is not None
            else {}
        ),
        "updated_at": parent.updated_at.isoformat() if parent.updated_at else None,
        "finished_at": parent.finished_at.isoformat() if parent.finished_at else None,
    }


def _fallback_pm_synthesis(row: ProjectAutonomyRun, expert_threads: list[dict[str, Any]]) -> dict[str, Any]:
    if not expert_threads:
        return {}
    blocked = [
        str(thread.get("display_name") or thread.get("name") or thread.get("profile_key"))
        for thread in expert_threads
        if str(thread.get("status") or "").lower() in {"blocked", "failed"}
    ]
    next_action = (
        "Resolve blocked expert threads before approval."
        if blocked
        else "Review the plan and expert handoffs; approval still gates any file changes."
    )
    return {
        "mode": EXPERT_WORKFLOW_MODE_PM_LED,
        "summary": f"PM coordinated {len(expert_threads)} expert thread(s) for this Project Autopilot run.",
        "decisions": [
            "Project-domain coordination only.",
            "Implementation remains behind the existing approval and worktree gates.",
        ],
        "blockers": blocked,
        "safety_gates": _thread_safety_gate_summary(expert_threads),
        "next_action": next_action,
        "status": row.status,
    }


def run_payload(db: Session, row: ProjectAutonomyRun, *, include_events: bool = False) -> dict[str, Any]:
    payload = _run_payload(row)
    raw_agents = payload.get("agents") if isinstance(payload.get("agents"), list) else []
    expert_threads = _expert_threads_payload(db, row, raw_agents)
    payload["expert_threads"] = expert_threads
    payload["parent_run"] = _parent_run_summary(db, row)
    payload["pm_synthesis"] = _latest_artifact_json(
        db,
        row.run_id,
        artifact_type="pm_synthesis",
        name="pm_synthesis",
    ) or _fallback_pm_synthesis(row, expert_threads)
    latest_review = _latest_architect_review(db, row.run_id)
    review_payload = _architect_review_payload(latest_review)
    if review_payload:
        review_payload["stale"] = not _architect_review_current_for_plan(
            latest_review,
            prompt=row.prompt,
            plan=_json_load(row.plan_json, {}),
        )
    payload["architect_review"] = review_payload
    if row.agent_profile_id is not None:
        profile = db.get(ProjectAutonomyAgentProfile, int(row.agent_profile_id))
        payload["agent_profile"] = _agent_profile_payload(db, profile) if profile is not None else {}
    else:
        payload["agent_profile"] = {}
    payload["operator_questions"] = [
        _operator_question_payload(question)
        for question in (
            db.query(ProjectAutonomyOperatorQuestion)
            .filter(ProjectAutonomyOperatorQuestion.run_id == row.run_id)
            .order_by(ProjectAutonomyOperatorQuestion.id.asc())
            .limit(50)
            .all()
        )
    ]
    payload["delegations"] = [
        _delegation_payload(delegation)
        for delegation in (
            db.query(ProjectAutonomyDelegation)
            .filter(ProjectAutonomyDelegation.parent_run_id == row.run_id)
            .order_by(ProjectAutonomyDelegation.id.asc())
            .limit(100)
            .all()
        )
    ]
    payload["child_runs"] = [
        child.run_id
        for child in (
            db.query(ProjectAutonomyRun)
            .filter(ProjectAutonomyRun.parent_run_id == row.run_id)
            .order_by(ProjectAutonomyRun.id.asc())
            .limit(100)
            .all()
        )
    ]
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
        payload["artifacts"] = [
            _artifact_payload(artifact)
            for artifact in (
                db.query(ProjectAutonomyArtifact)
                .filter(ProjectAutonomyArtifact.run_id == row.run_id)
                .order_by(ProjectAutonomyArtifact.id.asc())
                .limit(80)
                .all()
            )
        ]
    return payload


def list_runs(
    db: Session,
    *,
    user_id: int | None = None,
    repo_id: int | None = None,
    agent_profile_id: int | None = None,
    include_archived: bool = False,
    include_child_runs: bool = False,
    limit: int = 20,
) -> list[dict[str, Any]]:
    recover_orphaned_runs(db, user_id=user_id)
    q = db.query(ProjectAutonomyRun)
    if user_id is not None:
        q = q.filter(ProjectAutonomyRun.user_id == user_id)
    if repo_id is not None:
        q = q.filter(ProjectAutonomyRun.repo_id == int(repo_id))
    if agent_profile_id is not None:
        q = q.filter(ProjectAutonomyRun.agent_profile_id == int(agent_profile_id))
    if not include_child_runs:
        q = q.filter(ProjectAutonomyRun.parent_run_id.is_(None))
    if not include_archived:
        q = q.filter(ProjectAutonomyRun.archived_at.is_(None))
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


def update_agent_profile(
    db: Session,
    profile_id: int,
    *,
    user_id: int | None = None,
    status: str | None = None,
    model_policy: str | None = None,
    prompt_setting: dict[str, Any] | None = None,
    permissions: dict[str, Any] | None = None,
    schedule_enabled: bool | None = None,
    schedule: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    profile = _get_agent_profile(db, profile_id, user_id=user_id)
    if profile is None:
        return None
    if status is not None:
        clean_status = str(status).strip().lower()
        if clean_status not in {AGENT_PROFILE_STATUS_ACTIVE, AGENT_PROFILE_STATUS_PAUSED}:
            raise ValueError("Agent status must be active or paused.")
        profile.status = clean_status
    if model_policy is not None and str(model_policy).strip():
        profile.model_policy = str(model_policy).strip()[:40]
    if prompt_setting is not None:
        current = _json_load(profile.prompt_setting_json, {})
        if isinstance(current, dict):
            current.update(prompt_setting)
        else:
            current = dict(prompt_setting)
        if "system_prompt" in prompt_setting:
            current[CODEX_AUTOMATION_OPERATOR_MODIFIED_KEY] = True
        profile.prompt_setting_json = _json_text(current)
    if permissions is not None:
        current_permissions = _json_load(profile.permissions_json, {})
        if not isinstance(current_permissions, dict):
            current_permissions = dict(DEFAULT_AGENT_PERMISSIONS)
        for key in DEFAULT_AGENT_PERMISSIONS:
            if key in permissions:
                current_permissions[key] = bool(permissions[key])
        profile.permissions_json = _json_text(current_permissions)
    if schedule_enabled is not None:
        profile.schedule_enabled = bool(schedule_enabled)
    if schedule is not None:
        current_schedule = _json_load(profile.schedule_json, {})
        if not isinstance(current_schedule, dict):
            current_schedule = dict(DEFAULT_AGENT_SCHEDULE)
        current_schedule.update(schedule)
        profile.schedule_json = _json_text(current_schedule)
    _sync_agent_schedule_row(db, profile)
    profile.updated_at = _utcnow()
    db.commit()
    db.refresh(profile)
    return _agent_profile_payload(db, profile)


def pause_agent_profile(
    db: Session,
    profile_id: int,
    *,
    user_id: int | None = None,
) -> dict[str, Any] | None:
    return update_agent_profile(
        db,
        profile_id,
        user_id=user_id,
        status=AGENT_PROFILE_STATUS_PAUSED,
        schedule_enabled=False,
    )


def resume_agent_profile(
    db: Session,
    profile_id: int,
    *,
    user_id: int | None = None,
) -> dict[str, Any] | None:
    return update_agent_profile(
        db,
        profile_id,
        user_id=user_id,
        status=AGENT_PROFILE_STATUS_ACTIVE,
    )


def start_agent_cycle(
    db: Session,
    profile_id: int,
    *,
    user_id: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    profile = _get_agent_profile(db, profile_id, user_id=user_id)
    if profile is None:
        return None
    if profile.status != AGENT_PROFILE_STATUS_ACTIVE:
        raise ValueError("Agent is paused. Resume it before starting a cycle.")
    permissions = _json_load(profile.permissions_json, {})
    if not isinstance(permissions, dict):
        permissions = dict(DEFAULT_AGENT_PERMISSIONS)
    prompt = (
        f"{profile.name} scheduled cycle: inspect the repo, summarize current risks, "
        "draft safe next steps, and ask the operator before implementation."
    )
    run = create_run(
        db,
        prompt=prompt,
        repo_id=int(profile.repo_id),
        user_id=user_id,
        agent_profile_id=int(profile.id),
        autonomy_level=AUTONOMY_LEVEL_SCHEDULED_AGENT,
        model_policy=profile.model_policy,
        execution_mode=EXECUTION_MODE_PLAN_APPROVAL,
        start_planning=bool(permissions.get(AGENT_PERMISSION_PLAN, True)),
    )
    _mark_agent_schedule_cycle_started(db, profile, now=now)
    db.commit()
    return run_payload(db, run, include_events=True)


def run_due_agent_cycles(
    db: Session,
    *,
    user_id: int | None = None,
    now: datetime | None = None,
    limit: int = AGENT_SCHEDULE_DUE_CYCLE_LIMIT,
) -> dict[str, Any]:
    """Create bounded plan-first runs for active agent schedules that are due."""
    due_at = now or _utcnow()
    max_cycles = max(AGENT_SCHEDULE_MIN_INTERVAL, int(limit or AGENT_SCHEDULE_DUE_CYCLE_LIMIT))
    rows = (
        db.query(ProjectAutonomyAgentSchedule, ProjectAutonomyAgentProfile)
        .join(
            ProjectAutonomyAgentProfile,
            ProjectAutonomyAgentProfile.id == ProjectAutonomyAgentSchedule.profile_id,
        )
        .filter(
            ProjectAutonomyAgentProfile.status == AGENT_PROFILE_STATUS_ACTIVE,
            ProjectAutonomyAgentProfile.schedule_enabled.is_(True),
            ProjectAutonomyAgentSchedule.status == AGENT_SCHEDULE_STATUS_ACTIVE,
        )
        .order_by(ProjectAutonomyAgentSchedule.next_run_at.asc())
        .all()
    )
    runs: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for schedule, profile in rows:
        if user_id is not None and profile.user_id != user_id:
            continue
        schedule_config = _agent_schedule_config_for_profile(profile)
        always_on = _agent_runtime_is_always_on(schedule_config)
        if _agent_schedule_interval(schedule.rrule) is None and not always_on:
            continue
        if schedule.next_run_at is not None and schedule.next_run_at > due_at:
            continue
        rest_skip = _agent_runtime_rest_gate(profile, schedule, now=due_at)
        if rest_skip is not None:
            skipped.append(rest_skip)
            db.commit()
            continue
        repo = db.get(CodeRepo, int(profile.repo_id))
        if (
            repo is not None
            and cb_indexer.is_current_workspace_repo(repo)
            and not cb_indexer.is_preferred_autopilot_repo(repo)
        ):
            schedule.next_run_at = (
                due_at if always_on else _next_agent_schedule_run_at(schedule.rrule, from_time=due_at)
            )
            schedule.updated_at = due_at
            skipped.append(
                {
                    "profile_id": profile.id,
                    "profile_key": profile.profile_key,
                    "repo_id": profile.repo_id,
                    "reason": AGENT_SCHEDULE_SKIP_REPO_ALIAS,
                    "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None,
                }
            )
            db.commit()
            continue
        if _profile_has_open_scheduled_cycle(db, int(profile.id)):
            schedule.next_run_at = (
                due_at if always_on else _next_agent_schedule_run_at(schedule.rrule, from_time=due_at)
            )
            schedule.updated_at = due_at
            skipped.append(
                {
                    "profile_id": profile.id,
                    "profile_key": profile.profile_key,
                    "reason": AGENT_SCHEDULE_SKIP_OPEN_CYCLE,
                    "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None,
                }
            )
            db.commit()
            continue
        payload = start_agent_cycle(db, int(profile.id), user_id=profile.user_id, now=due_at)
        if payload is not None:
            runs.append(payload)
        if len(runs) >= max_cycles:
            break
    return {
        "started": len(runs),
        "skipped": skipped,
        "runs": runs,
        "checked": len(rows),
    }


def run_agent_schedules_now(
    db: Session,
    *,
    repo_id: int,
    user_id: int | None = None,
    codex_only: bool = True,
    limit: int = AGENT_SCHEDULE_DUE_CYCLE_LIMIT,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Mark active repo schedules due now, then start bounded plan-only cycles."""
    due_at = now or _utcnow()
    max_cycles = max(AGENT_SCHEDULE_MIN_INTERVAL, int(limit or AGENT_SCHEDULE_DUE_CYCLE_LIMIT))
    rows = (
        db.query(ProjectAutonomyAgentSchedule, ProjectAutonomyAgentProfile)
        .join(
            ProjectAutonomyAgentProfile,
            ProjectAutonomyAgentProfile.id == ProjectAutonomyAgentSchedule.profile_id,
        )
        .filter(
            ProjectAutonomyAgentProfile.repo_id == int(repo_id),
            ProjectAutonomyAgentProfile.status == AGENT_PROFILE_STATUS_ACTIVE,
            ProjectAutonomyAgentProfile.schedule_enabled.is_(True),
            ProjectAutonomyAgentSchedule.status == AGENT_SCHEDULE_STATUS_ACTIVE,
        )
        .order_by(ProjectAutonomyAgentSchedule.next_run_at.asc())
        .all()
    )
    woken = 0
    for schedule, profile in rows:
        if user_id is not None and profile.user_id != user_id:
            continue
        if codex_only:
            prompt_setting = _json_load(profile.prompt_setting_json, {})
            if not isinstance(prompt_setting, Mapping) or prompt_setting.get("source") != CODEX_AUTOMATION_SOURCE:
                continue
        schedule_config = _agent_schedule_config_for_profile(profile)
        if _agent_schedule_interval(schedule.rrule) is None and not _agent_runtime_is_always_on(schedule_config):
            continue
        schedule.next_run_at = due_at
        schedule.updated_at = due_at
        woken += 1
        if woken >= max_cycles:
            break
    db.commit()
    result = run_due_agent_cycles(
        db,
        user_id=user_id,
        now=due_at,
        limit=max_cycles,
    )
    result.update(
        {
            "woken": woken,
            "codex_only": bool(codex_only),
            "repo_id": int(repo_id),
            "message": (
                f"Queued {result.get('started', 0)} active "
                f"{'Codex ' if codex_only else ''}agent cycle(s) now."
            ),
        }
    )
    return result


def archive_runs(
    db: Session,
    *,
    user_id: int | None = None,
    repo_id: int | None = None,
    agent_profile_id: int | None = None,
    reason: str = ARCHIVE_REASON_OPERATOR_CLEAR,
) -> dict[str, Any]:
    q = db.query(ProjectAutonomyRun).filter(ProjectAutonomyRun.archived_at.is_(None))
    if user_id is not None:
        q = q.filter(ProjectAutonomyRun.user_id == user_id)
    if repo_id is not None:
        q = q.filter(ProjectAutonomyRun.repo_id == int(repo_id))
    if agent_profile_id is not None:
        q = q.filter(ProjectAutonomyRun.agent_profile_id == int(agent_profile_id))
    rows = q.all()
    now = _utcnow()
    for row in rows:
        row.archived_at = now
        row.archive_reason = reason
        row.updated_at = now
    db.commit()
    return {"archived": len(rows), "reason": reason}


def record_operator_question(
    db: Session,
    run: ProjectAutonomyRun,
    question: str,
    *,
    context: dict[str, Any] | None = None,
    commit: bool = False,
) -> ProjectAutonomyOperatorQuestion:
    row = ProjectAutonomyOperatorQuestion(
        run_id=run.run_id,
        agent_profile_id=run.agent_profile_id,
        user_id=run.user_id,
        repo_id=run.repo_id,
        question=question,
        context_json=_json_text(context or {}),
        status=OPERATOR_QUESTION_STATUS_PENDING,
    )
    db.add(row)
    if commit:
        db.commit()
        db.refresh(row)
    return row


def _answer_pending_operator_questions(
    db: Session,
    run: ProjectAutonomyRun,
    answer: str,
    *,
    commit: bool = False,
) -> int:
    clean = (answer or "").strip()
    if not clean:
        return 0
    rows = (
        db.query(ProjectAutonomyOperatorQuestion)
        .filter(
            ProjectAutonomyOperatorQuestion.run_id == run.run_id,
            ProjectAutonomyOperatorQuestion.status == OPERATOR_QUESTION_STATUS_PENDING,
        )
        .order_by(ProjectAutonomyOperatorQuestion.id.asc())
        .all()
    )
    if not rows:
        return 0
    now = _utcnow()
    for row in rows:
        row.status = OPERATOR_QUESTION_STATUS_ANSWERED
        row.answer = clean
        row.answered_at = now
    run.updated_at = now
    if commit:
        db.commit()
    return len(rows)


def _latest_architect_review(db: Session, run_id: str) -> ProjectAutonomyArchitectReview | None:
    return (
        db.query(ProjectAutonomyArchitectReview)
        .filter(ProjectAutonomyArchitectReview.run_id == run_id)
        .order_by(ProjectAutonomyArchitectReview.id.desc())
        .first()
    )


def _architect_review_scope_fingerprint(prompt: str | None, plan: dict[str, Any] | None) -> str:
    payload = {
        "prompt": prompt or "",
        "plan": plan or {},
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _architect_review_scope_from_payload(review: ProjectAutonomyArchitectReview | dict[str, Any] | None) -> str:
    if review is None:
        return ""
    if isinstance(review, ProjectAutonomyArchitectReview):
        critique = _json_load(review.critique_json, {})
    else:
        critique = review.get("critique") or {}
    return str((critique or {}).get("scope_fingerprint") or "")


def _architect_review_current_for_plan(
    review: ProjectAutonomyArchitectReview | dict[str, Any] | None,
    *,
    prompt: str | None,
    plan: dict[str, Any] | None,
) -> bool:
    expected = _architect_review_scope_fingerprint(prompt, plan)
    actual = _architect_review_scope_from_payload(review)
    return bool(actual) and actual == expected


def _approved_plan_review_blocker(db: Session, run: ProjectAutonomyRun, plan: dict[str, Any], *, action: str) -> str | None:
    review = _latest_architect_review(db, run.run_id)
    if not _architect_review_passed(review):
        return f"Architect quality gate has not passed. Revise the plan before {action}."
    if not _architect_review_current_for_plan(review, prompt=run.prompt, plan=plan):
        return f"Architect quality gate is stale. Revise the plan before {action}."
    permission_blocker = _worktree_permission_blocker(run, action=action)
    if permission_blocker:
        return permission_blocker
    return None


def _worktree_permission_blocker(run: ProjectAutonomyRun, *, action: str) -> str | None:
    if run.autonomy_level != AUTONOMY_LEVEL_SCHEDULED_AGENT:
        return None
    snapshot = _json_load(run.agent_snapshot_json, {})
    permissions = snapshot.get("permissions") if isinstance(snapshot, dict) else {}
    if isinstance(permissions, dict) and permissions.get(AGENT_PERMISSION_WORKTREE) is True:
        return None
    return (
        "Agent permission gate has not passed. This scheduled agent can draft plans "
        "but cannot edit files until worktree permission is enabled. Enable worktree "
        f"permission and start a fresh cycle before {action}."
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
    user_id: int | None = None,
    after_message_id: int = 0,
    after_step_id: int = 0,
    after_artifact_id: int = 0,
) -> dict[str, Any]:
    run = _get_run_row(db, run_id, user_id=user_id)
    if user_id is not None and run is None:
        return {
            "run": None,
            "messages": [],
            "steps": [],
            "artifacts": [],
            "after_message_id": int(after_message_id or 0),
            "after_step_id": int(after_step_id or 0),
            "after_artifact_id": int(after_artifact_id or 0),
        }
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
        "run": run_payload(db, run, include_events=False) if run is not None else None,
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


def _canonical_repo_for_autopilot(
    db: Session,
    repo: CodeRepo,
    *,
    user_id: int | None,
) -> CodeRepo:
    if not cb_indexer.is_current_workspace_repo(repo):
        return repo
    if cb_indexer.is_preferred_autopilot_repo(repo):
        return repo
    preferred = cb_indexer.ensure_current_workspace_repo(db, user_id=user_id)
    _pause_current_workspace_alias_profiles(db, preferred_repo_id=int(preferred.id), user_id=user_id)
    return preferred


def _pause_current_workspace_alias_profiles(
    db: Session,
    *,
    preferred_repo_id: int,
    user_id: int | None,
) -> int:
    alias_repos = [
        repo
        for repo in cb_indexer.get_accessible_repos(db, user_id=user_id, include_shared=True)
        if int(repo.id) != int(preferred_repo_id)
        and cb_indexer.is_current_workspace_repo(repo)
        and not cb_indexer.is_preferred_autopilot_repo(repo)
    ]
    if not alias_repos:
        return 0
    alias_repo_ids = [int(repo.id) for repo in alias_repos]
    q = db.query(ProjectAutonomyAgentProfile).filter(
        ProjectAutonomyAgentProfile.repo_id.in_(alias_repo_ids)
    )
    if user_id is None:
        q = q.filter(ProjectAutonomyAgentProfile.user_id.is_(None))
    else:
        q = q.filter(ProjectAutonomyAgentProfile.user_id == user_id)
    profiles = q.all()
    now = _utcnow()
    for profile in profiles:
        profile.status = AGENT_PROFILE_STATUS_PAUSED
        profile.schedule_enabled = False
        profile.updated_at = now
        schedule = (
            db.query(ProjectAutonomyAgentSchedule)
            .filter(ProjectAutonomyAgentSchedule.profile_id == profile.id)
            .order_by(ProjectAutonomyAgentSchedule.id.desc())
            .first()
        )
        if schedule is not None:
            schedule.status = AGENT_SCHEDULE_STATUS_PAUSED
            schedule.next_run_at = None
            schedule.updated_at = now
    db.commit()
    return len(profiles)


def _canonical_agent_profile_for_repo(
    db: Session,
    profile: ProjectAutonomyAgentProfile | None,
    repo: CodeRepo,
    *,
    user_id: int | None,
) -> ProjectAutonomyAgentProfile | None:
    if profile is None or int(profile.repo_id) == int(repo.id):
        return profile
    source_repo = db.get(CodeRepo, int(profile.repo_id))
    if (
        source_repo is None
        or not cb_indexer.is_current_workspace_repo(source_repo)
        or not cb_indexer.is_preferred_autopilot_repo(repo)
    ):
        return profile
    bootstrap_agent_profiles(db, repo_id=int(repo.id), user_id=user_id)
    return (
        _agent_profile_query(db, user_id=user_id, repo_id=int(repo.id))
        .filter(ProjectAutonomyAgentProfile.profile_key == profile.profile_key)
        .first()
        or profile
    )


def _agent_profile_query(db: Session, *, user_id: int | None, repo_id: int | None = None):
    q = db.query(ProjectAutonomyAgentProfile)
    if user_id is None:
        q = q.filter(ProjectAutonomyAgentProfile.user_id.is_(None))
    else:
        q = q.filter(ProjectAutonomyAgentProfile.user_id == user_id)
    if repo_id is not None:
        q = q.filter(ProjectAutonomyAgentProfile.repo_id == int(repo_id))
    return q


def _repo_matches_specialist(repo: CodeRepo, definition: dict[str, Any]) -> bool:
    tokens = tuple(str(token).lower() for token in definition.get("tokens", ()))
    if not tokens:
        return False
    haystack = f"{repo.name or ''} {repo.path or ''} {repo.host_path or ''}".lower()
    if any(token in haystack for token in tokens):
        return True
    runtime_path = resolve_repo_runtime_path(repo)
    if runtime_path is None:
        return False
    for rel in ("app/services/trading", "docs/STRATEGY"):
        if (runtime_path / rel).exists():
            return True
    return False


def _compact_identifier(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _slug_identifier(value: str, *, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug or fallback


def _content_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalized_path_text(value: str) -> str:
    return value.strip().lower().replace("\\", "/").rstrip("/")


def _codex_automation_roots() -> list[Path]:
    roots: list[Path] = []
    env_home = (os.environ.get("CODEX_HOME") or "").strip()
    if env_home:
        roots.append(Path(env_home))
    try:
        roots.append(Path.home() / ".codex")
    except Exception:
        pass
    seen: set[str] = set()
    unique: list[Path] = []
    for root in roots:
        key = str(root).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(root)
    return unique


def _codex_automation_config_paths() -> list[Path]:
    paths: list[Path] = []
    for root in _codex_automation_roots():
        automations = root / CODEX_AUTOMATION_CONFIG_DIR
        if not automations.is_dir():
            continue
        try:
            paths.extend(sorted(automations.glob(f"*/{CODEX_AUTOMATION_CONFIG_FILE}")))
        except OSError:
            continue
    return paths


def _codex_automation_declared_paths(data: dict[str, Any]) -> list[str]:
    raw = data.get(CODEX_AUTOMATION_CWDS_KEY)
    if isinstance(raw, str):
        values: Iterable[Any] = [raw]
    elif isinstance(raw, list):
        values = raw
    else:
        values = []
    paths: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text:
            paths.append(text)
    return paths


def _codex_automation_matches_repo(data: dict[str, Any], repo: CodeRepo) -> bool:
    prompt = str(data.get("prompt") or "")
    name = str(data.get("name") or "")
    haystack = f"{name}\n{prompt}".lower().replace("\\", "/")
    runtime_path = resolve_repo_runtime_path(repo)
    repo_paths = [
        _normalized_path_text(raw_path)
        for raw_path in (
            str(getattr(repo, "path", "") or ""),
            str(getattr(repo, "host_path", "") or ""),
            str(runtime_path) if runtime_path is not None else "",
        )
        if str(raw_path or "").strip()
    ]
    for declared_path in _codex_automation_declared_paths(data):
        normalized_declared = _normalized_path_text(declared_path)
        if not normalized_declared:
            continue
        declared_name = Path(normalized_declared).name.lower()
        if any(
            normalized_declared == repo_path
            or (declared_name and repo_path.endswith(f"/{declared_name}"))
            for repo_path in repo_paths
        ):
            return True
    for raw_path in (
        str(getattr(repo, "path", "") or ""),
        str(getattr(repo, "host_path", "") or ""),
    ):
        path = raw_path.strip()
        if path and _normalized_path_text(path) in haystack:
            return True
    repo_name = str(getattr(repo, "name", "") or "").strip()
    compact_repo_name = _compact_identifier(repo_name)
    if len(compact_repo_name) < CODEX_AUTOMATION_MIN_REPO_NAME_MATCH:
        return False
    if compact_repo_name in _compact_identifier(haystack):
        return True
    alias_tokens = CODEX_AUTOMATION_REPO_ALIAS_TOKENS.get(
        _slug_identifier(repo_name, fallback=""),
        (),
    )
    return bool(alias_tokens) and any(token in haystack for token in alias_tokens)


def _codex_automation_tier_and_role(identifier: str, name: str) -> tuple[str, str]:
    text = f"{identifier} {name}".lower()
    role = _slug_identifier(identifier, fallback="codex_agent")
    if any(token in text for token in ("project-manager", "agentops", "director", "manager", "lead")):
        return AGENT_PROFILE_TIER_MACRO, role
    if any(
        token in text
        for token in (
            "architect",
            "devops",
            "security",
            "compliance",
            "sre",
            "mlops",
            "data-scientist",
            "risk",
            "performance",
        )
    ):
        return AGENT_PROFILE_TIER_SPECIALIST, role
    return AGENT_PROFILE_TIER_MICRO, role


def _normalize_agent_schedule_rrule(rrule: str | None) -> str:
    normalized = str(rrule or "").strip()
    if normalized.upper().startswith(AGENT_SCHEDULE_RRULE_PREFIX):
        normalized = normalized[len(AGENT_SCHEDULE_RRULE_PREFIX) :].strip()
    return normalized


def _codex_automation_cadence(rrule: str) -> str:
    normalized = _normalize_agent_schedule_rrule(rrule).upper()
    parts = _agent_schedule_rrule_parts(normalized)
    freq = parts.get(AGENT_SCHEDULE_RRULE_FREQ_KEY)
    interval = parts.get(AGENT_SCHEDULE_RRULE_INTERVAL_KEY) or str(AGENT_SCHEDULE_DEFAULT_INTERVAL)
    if freq == AGENT_SCHEDULE_RRULE_FREQ_MINUTELY and interval == "2":
        return CODEX_AUTOMATION_CADENCE_TWO_MINUTES
    if freq == AGENT_SCHEDULE_RRULE_FREQ_MINUTELY and interval == "5":
        return CODEX_AUTOMATION_CADENCE_FIVE_MINUTES
    if freq == AGENT_SCHEDULE_RRULE_FREQ_MINUTELY and interval == "10":
        return CODEX_AUTOMATION_CADENCE_TEN_MINUTES
    if freq == AGENT_SCHEDULE_RRULE_FREQ_HOURLY and interval == "1":
        return CODEX_AUTOMATION_CADENCE_HOURLY
    return CODEX_AUTOMATION_CADENCE_MANUAL if not normalized else "custom"


def _codex_contract_value(prompt: str, label: str) -> str | None:
    match = re.search(rf"(?im)\b{re.escape(label)}\s*:\s*([^\n.]+)", prompt)
    if not match:
        return None
    value = truncate_text(match.group(1).strip(), CODEX_AUTOMATION_CONTRACT_VALUE_LIMIT)[0]
    return value or None


def _codex_contract_key_commands(prompt: str) -> list[str]:
    commands: list[str] = []
    seen: set[str] = set()
    for raw in re.findall(r"`([^`]+)`", prompt):
        command = " ".join(str(raw).strip().split())
        if not command:
            continue
        lower = command.lower()
        if not lower.startswith(CODEX_AUTOMATION_CONTRACT_COMMAND_PREFIXES):
            continue
        command = truncate_text(command, CODEX_AUTOMATION_CONTRACT_VALUE_LIMIT)[0]
        if command in seen:
            continue
        seen.add(command)
        commands.append(command)
        if len(commands) >= CODEX_AUTOMATION_CONTRACT_COMMAND_LIMIT:
            break
    return commands


def _codex_operating_contract(prompt: str, automation: Mapping[str, Any] | None = None) -> dict[str, Any]:
    clean_prompt = str(prompt or "")
    automation_payload = _mapping_payload(automation)
    contract: dict[str, Any] = {
        key: value
        for key, label in CODEX_AUTOMATION_CONTRACT_LABELS.items()
        if (value := _codex_contract_value(clean_prompt, label))
    }
    declared_paths = _codex_automation_declared_paths(dict(automation_payload))
    if declared_paths:
        contract["declared_paths"] = declared_paths[:CODEX_AUTOMATION_CONTRACT_DECLARED_PATH_LIMIT]
        contract.setdefault("workspace", declared_paths[0])
    rrule = str(automation_payload.get("rrule") or "").strip()
    status = str(automation_payload.get("status") or "").strip()
    if rrule:
        contract["source_rrule"] = rrule
        contract["cadence"] = _codex_automation_cadence(rrule)
    if status:
        contract["source_status"] = status
    commands = _codex_contract_key_commands(clean_prompt)
    if commands:
        contract["key_commands"] = commands
    lower_prompt = clean_prompt.lower()
    normalized_prompt = _normalized_path_text(clean_prompt)
    normalized_workspace = _normalized_path_text(str(contract.get("workspace") or ""))
    contract["d_drive_aligned"] = (
        _normalized_path_text(CODEX_AUTOMATION_D_DRIVE_ROOT) in normalized_prompt
        or _normalized_path_text(CODEX_AUTOMATION_D_DRIVE_ROOT) in normalized_workspace
    )
    if contract["d_drive_aligned"] and not contract.get("workspace"):
        contract["workspace"] = CODEX_AUTOMATION_D_DRIVE_ROOT
    contract["uses_mailbox_protocol"] = "agent_mailbox_protocol.md" in lower_prompt
    contract["uses_run_lock"] = "run.lock" in lower_prompt
    contract["requires_out_report"] = "out report" in lower_prompt or "\\out" in lower_prompt
    contract["uses_pr_review_flow"] = "draft pr" in lower_prompt or "approved_to_push" in lower_prompt
    safety_boundaries: list[str] = []
    for needle, label in CODEX_AUTOMATION_CONTRACT_SAFETY_ACTIONS:
        if needle in lower_prompt and label not in safety_boundaries:
            safety_boundaries.append(label)
    if safety_boundaries:
        contract["safety_boundaries"] = safety_boundaries
    return contract


def _codex_operating_contract_from_prompt_setting(prompt_setting: Mapping[str, Any]) -> dict[str, Any]:
    automation = _mapping_payload(prompt_setting.get("codex_automation"))
    existing = _mapping_payload(automation.get("operating_contract"))
    prompt = str(prompt_setting.get("system_prompt") or "")
    if not prompt and existing:
        return existing
    generated = _codex_operating_contract(prompt, automation)
    return {**generated, **existing}


def _codex_automation_definition(data: dict[str, Any], path: Path) -> dict[str, Any] | None:
    identifier = str(data.get("id") or path.parent.name).strip()
    name = str(data.get("name") or identifier).strip()
    prompt = str(data.get("prompt") or "").strip()
    if not identifier or not prompt:
        return None
    prompt_sha256 = _content_sha256(prompt)
    profile_key = (
        CODEX_AUTOMATION_PROFILE_PREFIX
        + _slug_identifier(identifier, fallback=CODEX_AUTOMATION_SOURCE)
    )[:CODEX_AUTOMATION_PROFILE_KEY_LIMIT].rstrip("_")
    tier, role = _codex_automation_tier_and_role(identifier, name)
    source_rrule = str(data.get("rrule") or "").strip()
    rrule = _normalize_agent_schedule_rrule(source_rrule)
    source_status = str(data.get("status") or "").strip()
    kind = str(data.get("kind") or "").strip()
    operating_contract = _codex_operating_contract(prompt, data)
    schedule = {
        "enabled": False,
        "rrule": rrule or None,
        "cadence": _codex_automation_cadence(rrule),
        "source_status": source_status,
        "budget": dict(DEFAULT_AGENT_SCHEDULE["budget"]),
    }
    return {
        "profile_key": profile_key,
        "name": name,
        "role": role,
        "tier": tier,
        "prompt": truncate_text(prompt, CODEX_AUTOMATION_PROMPT_LIMIT)[0],
        "source": CODEX_AUTOMATION_SOURCE,
        "schedule": schedule,
        "codex_automation": {
            "id": identifier,
            "name": name,
            "kind": kind,
            "status": source_status,
            "rrule": source_rrule,
            "normalized_rrule": rrule,
            "path": str(path),
            CODEX_AUTOMATION_CWDS_KEY: _codex_automation_declared_paths(data),
            CODEX_AUTOMATION_PROMPT_HASH_KEY: prompt_sha256,
            "prompt_length": len(prompt),
            "hash_algorithm": CODEX_AUTOMATION_HASH_ALGORITHM,
            "operating_contract": operating_contract,
        },
        "operating_contract": operating_contract,
    }


def _codex_repo_workspace_fallback(repo: CodeRepo) -> str | None:
    runtime_path = resolve_repo_runtime_path(repo)
    candidates = (
        str(runtime_path) if runtime_path is not None else "",
        str(getattr(repo, "host_path", "") or ""),
        str(getattr(repo, "path", "") or ""),
    )
    for candidate in candidates:
        clean = candidate.strip()
        if clean:
            return clean
    return None


def _apply_codex_contract_repo_workspace_fallback(
    contract: dict[str, Any],
    repo: CodeRepo,
) -> None:
    if contract.get("workspace"):
        return
    workspace = _codex_repo_workspace_fallback(repo)
    if not workspace:
        return
    contract["workspace"] = workspace
    contract["workspace_inferred_from_repo"] = True
    if _normalized_path_text(CODEX_AUTOMATION_D_DRIVE_ROOT) in _normalized_path_text(workspace):
        contract["d_drive_aligned"] = True


def _apply_codex_repo_workspace_fallback(
    definition: dict[str, Any],
    repo: CodeRepo,
) -> None:
    contract = definition.get("operating_contract")
    if not isinstance(contract, dict):
        return
    _apply_codex_contract_repo_workspace_fallback(contract, repo)
    automation = definition.get("codex_automation")
    if isinstance(automation, dict):
        automation["operating_contract"] = dict(contract)


def _codex_automation_definitions_for_repo(repo: CodeRepo) -> list[dict[str, Any]]:
    definitions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in _codex_automation_config_paths():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        if not isinstance(data, dict) or not _codex_automation_matches_repo(data, repo):
            continue
        definition = _codex_automation_definition(data, path)
        if definition is None:
            continue
        _apply_codex_repo_workspace_fallback(definition, repo)
        key = str(definition["profile_key"])
        if key in seen:
            continue
        seen.add(key)
        definitions.append(definition)
    return definitions


def _agent_definitions_for_repo(repo: CodeRepo) -> list[dict[str, Any]]:
    definitions = [dict(item) for item in DEFAULT_AGENT_PROFILE_DEFINITIONS]
    for specialist in SPECIALIST_AGENT_PROFILE_DEFINITIONS:
        if _repo_matches_specialist(repo, specialist):
            clean = {key: value for key, value in specialist.items() if key != "tokens"}
            definitions.append(clean)
    definitions.extend(_codex_automation_definitions_for_repo(repo))
    return definitions


def _codex_automation_definitions_by_key(repo: CodeRepo) -> dict[str, dict[str, Any]]:
    return {
        str(definition["profile_key"]): definition
        for definition in _codex_automation_definitions_for_repo(repo)
    }


def _agent_prompt_setting(definition: dict[str, Any], repo: CodeRepo) -> dict[str, Any]:
    setting = {
        "system_prompt": definition["prompt"],
        "repo_name": repo.name,
        "approval_first": True,
        "default_state": AGENT_PROFILE_STATUS_PAUSED,
    }
    source = definition.get("source")
    if source:
        setting["source"] = source
    automation = definition.get("codex_automation")
    if isinstance(automation, dict):
        setting["codex_automation"] = dict(automation)
    return setting


def _agent_profile_identity_text(profile: ProjectAutonomyAgentProfile) -> str:
    return " ".join(
        str(part or "").replace("_", " ").replace("-", " ").lower()
        for part in (profile.profile_key, profile.name, profile.role)
    )


def _default_supervisor_key_for_profile(profile: ProjectAutonomyAgentProfile) -> str | None:
    key = str(profile.profile_key or "")
    if key == PM_COORDINATOR_PROFILE_KEY:
        return None
    if profile.tier == AGENT_PROFILE_TIER_MACRO:
        return PM_COORDINATOR_PROFILE_KEY
    if key in {supervisor_key for supervisor_key, _tokens in AGENT_DEFAULT_SUPERVISOR_RULES}:
        return PM_COORDINATOR_PROFILE_KEY
    haystack = _agent_profile_identity_text(profile)
    for supervisor_key, tokens in AGENT_DEFAULT_SUPERVISOR_RULES:
        if supervisor_key == key:
            continue
        if any(token in haystack for token in tokens):
            return supervisor_key
    return "dev_lead"


def _rebalance_generated_agent_parents(
    profiles_by_key: Mapping[str, ProjectAutonomyAgentProfile],
) -> None:
    profiles_by_id = {
        int(profile.id): profile
        for profile in profiles_by_key.values()
        if profile.id is not None
    }
    fallback = profiles_by_key.get(PM_COORDINATOR_PROFILE_KEY) or profiles_by_key.get("architect")
    for profile in profiles_by_key.values():
        if not profile.generated:
            continue
        supervisor_key = _default_supervisor_key_for_profile(profile)
        supervisor = profiles_by_key.get(supervisor_key or "") or fallback
        if supervisor is None or supervisor.id == profile.id:
            continue
        current_parent = profiles_by_id.get(int(profile.parent_profile_id or 0))
        current_key = str(current_parent.profile_key) if current_parent is not None else None
        should_update = (
            profile.parent_profile_id is None
            or current_key in LEGACY_FLAT_SUPERVISOR_KEYS
            or current_key == supervisor.profile_key
        )
        if should_update and profile.parent_profile_id != supervisor.id:
            profile.parent_profile_id = supervisor.id


def _codex_profile_sync_state(
    profile: ProjectAutonomyAgentProfile,
    definition: dict[str, Any] | None,
) -> dict[str, Any]:
    prompt_setting = _json_load(profile.prompt_setting_json, {})
    if not isinstance(prompt_setting, dict):
        prompt_setting = {}
    codex_automation = prompt_setting.get("codex_automation")
    is_codex = (
        prompt_setting.get("source") == CODEX_AUTOMATION_SOURCE
        or isinstance(codex_automation, dict)
        or str(profile.profile_key).startswith(CODEX_AUTOMATION_PROFILE_PREFIX)
    )
    if not is_codex:
        return {"status": CODEX_AUTOMATION_SYNC_STATUS_NOT_CODEX, "reasons": []}
    if prompt_setting.get(CODEX_AUTOMATION_OPERATOR_MODIFIED_KEY):
        return {"status": CODEX_AUTOMATION_SYNC_STATUS_CUSTOM, "reasons": []}
    if definition is None:
        return {
            "status": CODEX_AUTOMATION_SYNC_STATUS_MISSING_SOURCE,
            "reasons": [CODEX_AUTOMATION_SYNC_STATUS_MISSING_SOURCE],
        }

    latest_automation = definition.get("codex_automation")
    if not isinstance(latest_automation, dict):
        latest_automation = {}
    stored_automation = codex_automation if isinstance(codex_automation, dict) else {}
    latest_prompt_hash = str(latest_automation.get(CODEX_AUTOMATION_PROMPT_HASH_KEY) or "")
    stored_prompt_hash = str(stored_automation.get(CODEX_AUTOMATION_PROMPT_HASH_KEY) or "")
    stored_prompt = str(prompt_setting.get("system_prompt") or "")
    latest_prompt = str(definition.get("prompt") or "")
    reasons: list[str] = []
    if stored_prompt_hash and latest_prompt_hash:
        if stored_prompt_hash != latest_prompt_hash:
            reasons.append(CODEX_AUTOMATION_SYNC_REASON_PROMPT)
    elif stored_prompt != latest_prompt:
        reasons.append(CODEX_AUTOMATION_SYNC_REASON_PROMPT)
    elif not stored_prompt_hash and latest_prompt_hash:
        reasons.append(CODEX_AUTOMATION_SYNC_REASON_MISSING_HASH)

    stored_schedule = _json_load(profile.schedule_json, {})
    if not isinstance(stored_schedule, dict):
        stored_schedule = {}
    latest_schedule = _agent_schedule_config(definition)
    if not profile.schedule_enabled:
        for key in ("rrule", "cadence"):
            if (stored_schedule.get(key) or None) != (latest_schedule.get(key) or None):
                reasons.append(CODEX_AUTOMATION_SYNC_REASON_SCHEDULE)
                break
        if (stored_schedule.get("source_status") or "") != (latest_schedule.get("source_status") or ""):
            reasons.append(CODEX_AUTOMATION_SYNC_REASON_STATUS)

    status = (
        CODEX_AUTOMATION_SYNC_STATUS_STALE
        if any(reason != CODEX_AUTOMATION_SYNC_REASON_MISSING_HASH for reason in reasons)
        else CODEX_AUTOMATION_SYNC_STATUS_CURRENT
    )
    return {
        "status": status,
        "reasons": reasons,
        "current_prompt_sha256": stored_prompt_hash or _content_sha256(stored_prompt) if stored_prompt else None,
        "source_prompt_sha256": latest_prompt_hash or None,
        "source_rrule": latest_schedule.get("rrule"),
        "source_status": latest_schedule.get("source_status"),
    }


def _sync_codex_profile_from_definition(
    db: Session,
    profile: ProjectAutonomyAgentProfile,
    definition: dict[str, Any],
    repo: CodeRepo,
) -> bool:
    state = _codex_profile_sync_state(profile, definition)
    if state["status"] != CODEX_AUTOMATION_SYNC_STATUS_STALE:
        if CODEX_AUTOMATION_SYNC_REASON_MISSING_HASH in state.get("reasons", []):
            profile.prompt_setting_json = _json_text(_agent_prompt_setting(definition, repo))
            return True
        return False
    profile.prompt_setting_json = _json_text(_agent_prompt_setting(definition, repo))
    if not profile.schedule_enabled:
        profile.schedule_json = _json_text(_agent_schedule_config(definition))
        _sync_agent_schedule_row(db, profile)
    profile.updated_at = _utcnow()
    return True


def _agent_profile_snapshot(profile: ProjectAutonomyAgentProfile | None) -> dict[str, Any]:
    if profile is None:
        return {}
    return {
        "id": profile.id,
        "profile_key": profile.profile_key,
        "name": profile.name,
        "role": profile.role,
        "tier": profile.tier,
        "status": profile.status,
        "model_policy": profile.model_policy,
        "permissions": _json_load(profile.permissions_json, {}),
        "prompt_setting": _json_load(profile.prompt_setting_json, {}),
    }


def _default_agent_schedule_config() -> dict[str, Any]:
    return {
        "enabled": False,
        "rrule": None,
        AGENT_RUNTIME_MODE_KEY: AGENT_RUNTIME_MODE_SCHEDULED,
        AGENT_RUNTIME_WORK_WINDOW_MINUTES_KEY: AGENT_RUNTIME_DEFAULT_WORK_WINDOW_MINUTES,
        AGENT_RUNTIME_REST_MINUTES_KEY: AGENT_RUNTIME_DEFAULT_REST_MINUTES,
        "budget": dict(DEFAULT_AGENT_SCHEDULE["budget"]),
    }


def _agent_schedule_config(definition: dict[str, Any]) -> dict[str, Any]:
    config = _default_agent_schedule_config()
    raw_schedule = definition.get("schedule")
    if isinstance(raw_schedule, dict):
        config.update(raw_schedule)
    budget = config.get("budget")
    config["budget"] = (
        dict(budget)
        if isinstance(budget, dict)
        else dict(DEFAULT_AGENT_SCHEDULE["budget"])
    )
    return config


def _codex_profile_source_rrule(profile: ProjectAutonomyAgentProfile) -> str | None:
    prompt_setting = _json_load(profile.prompt_setting_json, {})
    if not isinstance(prompt_setting, dict):
        prompt_setting = {}
    automation = prompt_setting.get("codex_automation")
    if not isinstance(automation, dict):
        automation = {}
    rrule = _normalize_agent_schedule_rrule(
        str(automation.get("normalized_rrule") or automation.get("rrule") or "")
    )
    return rrule or None


def _codex_profile_source_schedule_config(profile: ProjectAutonomyAgentProfile) -> dict[str, Any]:
    schedule_config = _agent_schedule_config_for_profile(profile)
    source_rrule = _codex_profile_source_rrule(profile)
    if source_rrule is not None:
        schedule_config["rrule"] = source_rrule
        schedule_config["cadence"] = _codex_automation_cadence(source_rrule)
    schedule_config["enabled"] = True
    schedule_config[AGENT_RUNTIME_MODE_KEY] = AGENT_RUNTIME_MODE_SCHEDULED
    schedule_config[AGENT_RUNTIME_WORK_STARTED_AT_KEY] = None
    schedule_config[AGENT_RUNTIME_REST_UNTIL_KEY] = None
    return schedule_config


def _codex_profile_always_on_schedule_config(profile: ProjectAutonomyAgentProfile) -> dict[str, Any]:
    schedule_config = _agent_schedule_config_for_profile(profile)
    source_rrule = _codex_profile_source_rrule(profile)
    if source_rrule is not None:
        schedule_config["source_rrule"] = source_rrule
    schedule_config["enabled"] = True
    schedule_config["rrule"] = None
    schedule_config["cadence"] = CODEX_AUTOMATION_CADENCE_ALWAYS_ON
    schedule_config[AGENT_RUNTIME_MODE_KEY] = AGENT_RUNTIME_MODE_ALWAYS_ON
    schedule_config[AGENT_RUNTIME_WORK_WINDOW_MINUTES_KEY] = AGENT_RUNTIME_DEFAULT_WORK_WINDOW_MINUTES
    schedule_config[AGENT_RUNTIME_REST_MINUTES_KEY] = AGENT_RUNTIME_DEFAULT_REST_MINUTES
    schedule_config[AGENT_RUNTIME_WORK_STARTED_AT_KEY] = None
    schedule_config[AGENT_RUNTIME_REST_UNTIL_KEY] = None
    return schedule_config


def _agent_schedule_payload(
    schedule: ProjectAutonomyAgentSchedule | None,
    *,
    stored_schedule: dict[str, Any] | None = None,
) -> dict[str, Any]:
    stored = _default_agent_schedule_config()
    if isinstance(stored_schedule, dict):
        stored.update(stored_schedule)
        budget = stored.get("budget")
        stored["budget"] = (
            dict(budget)
            if isinstance(budget, dict)
            else dict(DEFAULT_AGENT_SCHEDULE["budget"])
        )
    if schedule is None:
        return stored
    payload = {
        "id": schedule.id,
        "status": schedule.status,
        "rrule": schedule.rrule,
        "budget": _json_load(schedule.budget_json, {}),
        "last_run_at": schedule.last_run_at.isoformat() if schedule.last_run_at else None,
        "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None,
    }
    for key in (
        "enabled",
        "cadence",
        "source_status",
        "source_rrule",
        AGENT_RUNTIME_MODE_KEY,
        AGENT_RUNTIME_WORK_WINDOW_MINUTES_KEY,
        AGENT_RUNTIME_REST_MINUTES_KEY,
        AGENT_RUNTIME_WORK_STARTED_AT_KEY,
        AGENT_RUNTIME_REST_UNTIL_KEY,
    ):
        if key in stored:
            payload[key] = stored.get(key)
    if payload.get("rrule") is None and stored.get("rrule") is not None:
        payload["rrule"] = stored.get("rrule")
    return payload


def _agent_profile_operating_state(
    profile: ProjectAutonomyAgentProfile,
    *,
    schedule_payload: Mapping[str, Any],
    prompt_freshness: Mapping[str, Any],
    prompt_setting: Mapping[str, Any],
    permissions: Mapping[str, Any],
    open_run_count: int,
    pending_question_count: int,
) -> dict[str, Any]:
    can_patch = bool(permissions.get(AGENT_PERMISSION_WORKTREE))
    can_merge = bool(permissions.get(AGENT_PERMISSION_MERGE))
    safety = (
        AGENT_OPERATING_SAFETY_MERGE_CAPABLE
        if can_merge
        else AGENT_OPERATING_SAFETY_PATCH_CAPABLE
        if can_patch
        else AGENT_OPERATING_SAFETY_PLAN_ONLY
    )
    safety_detail = (
        "Merge permission is enabled; merge gates and validation still apply."
        if can_merge
        else "Worktree patching is enabled; merge remains separately gated."
        if can_patch
        else "Plan-only: this agent can observe, research, summarize, and ask before patching."
    )
    freshness_status = str(prompt_freshness.get("status") or "")
    automation = _mapping_payload(prompt_setting.get("codex_automation"))
    source_status = str(
        automation.get("status")
        or schedule_payload.get("source_status")
        or ""
    ).strip().upper()
    schedule_status = str(schedule_payload.get("status") or "")
    next_run_at = str(schedule_payload.get("next_run_at") or "").strip()

    if pending_question_count > 0:
        state = AGENT_OPERATING_STATE_NEEDS_INPUT
        title = "Needs operator input"
        detail = f"{pending_question_count} pending question(s) are blocking this agent."
        action = AGENT_OPERATING_ACTION_ANSWER_QUESTION
        action_label = "Answer in chat"
    elif freshness_status in {
        CODEX_AUTOMATION_SYNC_STATUS_STALE,
        CODEX_AUTOMATION_SYNC_STATUS_MISSING_SOURCE,
    }:
        state = AGENT_OPERATING_STATE_NEEDS_SYNC
        title = "Codex prompt needs sync"
        detail = "The stored CHILI prompt snapshot no longer matches the local Codex automation source."
        action = AGENT_OPERATING_ACTION_SYNC_CODEX
        action_label = "Sync Codex prompts"
    elif freshness_status == CODEX_AUTOMATION_SYNC_STATUS_CUSTOM:
        state = AGENT_OPERATING_STATE_CUSTOM_PROMPT
        title = "Custom prompt override"
        detail = "This agent has operator edits; Codex sync preserves the custom prompt instead of overwriting it."
        action = AGENT_OPERATING_ACTION_REVIEW_CUSTOM
        action_label = "Review prompt"
    elif open_run_count > 0:
        state = AGENT_OPERATING_STATE_RUNNING
        title = "Run in progress"
        detail = f"{open_run_count} open run(s) are already using this agent."
        action = AGENT_OPERATING_ACTION_OPEN_CHAT
        action_label = "Open chat"
    elif profile.status == AGENT_PROFILE_STATUS_PAUSED:
        if prompt_setting.get("source") == CODEX_AUTOMATION_SOURCE and source_status == "ACTIVE":
            state = AGENT_OPERATING_STATE_PAUSED_SOURCE_ACTIVE
            title = "Source-active Codex agent is paused"
            detail = "The Codex automation is active, but CHILI keeps the local mirror paused until you enable it."
            action = AGENT_OPERATING_ACTION_ENABLE_ACTIVE
            action_label = "Enable active"
        else:
            state = AGENT_OPERATING_STATE_PAUSED
            title = "Paused"
            detail = "This agent is dormant until you resume it or enable a schedule."
            action = AGENT_OPERATING_ACTION_RESUME
            action_label = "Resume"
    elif not profile.schedule_enabled:
        state = AGENT_OPERATING_STATE_MANUAL_READY
        title = "Manual chat ready"
        detail = "The agent is active for manual chats, but scheduled cycles are disabled."
        action = AGENT_OPERATING_ACTION_ENABLE_SCHEDULE
        action_label = "Enable schedule"
    elif schedule_status == AGENT_SCHEDULE_STATUS_ACTIVE:
        state = AGENT_OPERATING_STATE_SCHEDULED
        title = "Scheduled"
        detail = (
            f"Next cycle is queued for {next_run_at}."
            if next_run_at
            else "A schedule is active and will queue the next safe plan-only cycle."
        )
        action = AGENT_OPERATING_ACTION_WAIT_OR_RUN_NOW
        action_label = "Wait or run now"
    else:
        state = AGENT_OPERATING_STATE_READY
        title = "Ready"
        detail = "This agent is available for a new chat or plan-first cycle."
        action = AGENT_OPERATING_ACTION_START_CHAT
        action_label = "Start chat"

    return {
        "state": state,
        "title": title,
        "detail": detail,
        "next_action": action,
        "next_action_label": action_label,
        "safety": safety,
        "safety_detail": safety_detail,
        "source_status": source_status or None,
        "open_run_count": open_run_count,
        "pending_question_count": pending_question_count,
        "can_patch": can_patch,
        "can_merge": can_merge,
    }


def _agent_profile_payload(
    db: Session,
    profile: ProjectAutonomyAgentProfile,
    *,
    codex_definition: dict[str, Any] | None = None,
) -> dict[str, Any]:
    repo: CodeRepo | None = None
    if profile.repo_id is not None:
        repo = db.get(CodeRepo, int(profile.repo_id))
    if codex_definition is None and repo is not None:
        codex_definition = _codex_automation_definitions_by_key(repo).get(str(profile.profile_key))
    schedule = (
        db.query(ProjectAutonomyAgentSchedule)
        .filter(ProjectAutonomyAgentSchedule.profile_id == profile.id)
        .order_by(ProjectAutonomyAgentSchedule.id.desc())
        .first()
    )
    active_runs = (
        db.query(ProjectAutonomyRun)
        .filter(
            ProjectAutonomyRun.agent_profile_id == profile.id,
            ProjectAutonomyRun.archived_at.is_(None),
        )
        .count()
    )
    open_runs = (
        db.query(ProjectAutonomyRun)
        .filter(
            ProjectAutonomyRun.agent_profile_id == profile.id,
            ProjectAutonomyRun.archived_at.is_(None),
            ~ProjectAutonomyRun.status.in_(list(TERMINAL_STATUSES)),
        )
        .count()
    )
    pending_questions = (
        db.query(ProjectAutonomyOperatorQuestion)
        .filter(
            ProjectAutonomyOperatorQuestion.agent_profile_id == profile.id,
            ProjectAutonomyOperatorQuestion.status == OPERATOR_QUESTION_STATUS_PENDING,
        )
        .count()
    )
    prompt_freshness = _codex_profile_sync_state(profile, codex_definition)
    prompt_setting = _json_load(profile.prompt_setting_json, {})
    if not isinstance(prompt_setting, dict):
        prompt_setting = {}
    permissions = _json_load(profile.permissions_json, {})
    if not isinstance(permissions, dict):
        permissions = {}
    schedule_payload = _agent_schedule_payload(
        schedule,
        stored_schedule=_json_load(profile.schedule_json, {}),
    )
    payload = {
        "id": profile.id,
        "user_id": profile.user_id,
        "repo_id": profile.repo_id,
        "profile_key": profile.profile_key,
        "name": profile.name,
        "role": profile.role,
        "tier": profile.tier,
        "status": profile.status,
        "model_policy": profile.model_policy,
        "prompt_setting": prompt_setting,
        "permissions": permissions,
        "schedule_enabled": bool(profile.schedule_enabled),
        "schedule": schedule_payload,
        "parent_profile_id": profile.parent_profile_id,
        "generated": bool(profile.generated),
        "active_run_count": active_runs,
        "open_run_count": open_runs,
        "pending_question_count": pending_questions,
        "created_at": profile.created_at.isoformat() if profile.created_at else None,
        "updated_at": profile.updated_at.isoformat() if profile.updated_at else None,
    }
    payload["operating_state"] = _agent_profile_operating_state(
        profile,
        schedule_payload=schedule_payload,
        prompt_freshness=prompt_freshness,
        prompt_setting=prompt_setting,
        permissions=permissions,
        open_run_count=open_runs,
        pending_question_count=pending_questions,
    )
    if prompt_freshness["status"] != CODEX_AUTOMATION_SYNC_STATUS_NOT_CODEX:
        payload["prompt_freshness"] = prompt_freshness
        operating_contract = _codex_operating_contract_from_prompt_setting(prompt_setting)
        if repo is not None:
            _apply_codex_contract_repo_workspace_fallback(operating_contract, repo)
        payload["operating_contract"] = operating_contract
    return payload


def bootstrap_agent_profiles(
    db: Session,
    *,
    repo_id: int,
    user_id: int | None = None,
) -> list[dict[str, Any]]:
    repo = _resolve_repo_for_run(db, repo_id, user_id=user_id)
    if repo is None:
        raise ValueError("Repo not found.")
    repo = _canonical_repo_for_autopilot(db, repo, user_id=user_id)
    definitions = _agent_definitions_for_repo(repo)
    codex_definitions = _codex_automation_definitions_by_key(repo)
    existing = {
        profile.profile_key: profile
        for profile in _agent_profile_query(db, user_id=user_id, repo_id=int(repo.id)).all()
    }
    for definition in definitions:
        key = str(definition["profile_key"])
        profile = existing.get(key)
        schedule_config = _agent_schedule_config(definition)
        if profile is None:
            profile = ProjectAutonomyAgentProfile(
                user_id=user_id,
                repo_id=int(repo.id),
                profile_key=key,
                name=str(definition["name"]),
                role=str(definition["role"]),
                tier=str(definition["tier"]),
                status=AGENT_PROFILE_STATUS_PAUSED,
                model_policy="local_first",
                prompt_setting_json=_json_text(_agent_prompt_setting(definition, repo)),
                permissions_json=_json_text(DEFAULT_AGENT_PERMISSIONS),
                schedule_enabled=False,
                schedule_json=_json_text(schedule_config),
                generated=True,
            )
            db.add(profile)
            db.flush()
            db.add(
                ProjectAutonomyAgentSchedule(
                    profile_id=profile.id,
                    status=AGENT_SCHEDULE_STATUS_PAUSED,
                    rrule=schedule_config.get("rrule"),
                    budget_json=_json_text(
                        schedule_config.get("budget") or DEFAULT_AGENT_SCHEDULE["budget"]
                    ),
                )
            )
            existing[key] = profile
        elif key in codex_definitions:
            _sync_codex_profile_from_definition(db, profile, codex_definitions[key], repo)

    _rebalance_generated_agent_parents(existing)
    db.commit()
    profiles = _agent_profile_query(db, user_id=user_id, repo_id=int(repo.id)).order_by(
        ProjectAutonomyAgentProfile.tier.asc(),
        ProjectAutonomyAgentProfile.profile_key.asc(),
    ).all()
    return [
        _agent_profile_payload(
            db,
            profile,
            codex_definition=codex_definitions.get(str(profile.profile_key)),
        )
        for profile in profiles
    ]


def sync_codex_agent_profiles(
    db: Session,
    *,
    repo_id: int,
    user_id: int | None = None,
) -> dict[str, Any]:
    """Refresh CHILI's Codex-imported profiles from local automation.toml files."""
    repo = _resolve_repo_for_run(db, repo_id, user_id=user_id)
    if repo is None:
        raise ValueError("Repo not found.")
    repo = _canonical_repo_for_autopilot(db, repo, user_id=user_id)
    definitions = _codex_automation_definitions_by_key(repo)
    source_keys = set(definitions)
    before_profiles = _agent_profile_query(db, user_id=user_id, repo_id=int(repo.id)).all()
    before_keys = {str(profile.profile_key) for profile in before_profiles}
    refreshable_before: list[str] = []
    custom_before: list[str] = []
    for profile in before_profiles:
        key = str(profile.profile_key)
        prompt_setting = _json_load(profile.prompt_setting_json, {})
        if not isinstance(prompt_setting, Mapping):
            prompt_setting = {}
        is_codex = (
            key in source_keys
            or prompt_setting.get("source") == CODEX_AUTOMATION_SOURCE
            or key.startswith(CODEX_AUTOMATION_PROFILE_PREFIX)
        )
        if not is_codex:
            continue
        state = _codex_profile_sync_state(profile, definitions.get(key))
        status = str(state.get("status") or "")
        reasons = [str(reason) for reason in state.get("reasons") or []]
        if status == CODEX_AUTOMATION_SYNC_STATUS_CUSTOM:
            custom_before.append(key)
            continue
        if status == CODEX_AUTOMATION_SYNC_STATUS_STALE or CODEX_AUTOMATION_SYNC_REASON_MISSING_HASH in reasons:
            refreshable_before.append(key)

    agents = bootstrap_agent_profiles(db, repo_id=int(repo.id), user_id=user_id)
    current_codex = [
        profile
        for profile in agents
        if str(profile.get("profile_key") or "") in source_keys
    ]
    imported_codex = [
        profile
        for profile in agents
        if str(profile.get("profile_key") or "") in source_keys
        or _mapping_payload(profile.get("prompt_setting")).get("source") == CODEX_AUTOMATION_SOURCE
    ]
    stale_after = [
        profile
        for profile in current_codex
        if _mapping_payload(profile.get("prompt_freshness")).get("status")
        in {CODEX_AUTOMATION_SYNC_STATUS_STALE, CODEX_AUTOMATION_SYNC_STATUS_MISSING_SOURCE}
    ]
    custom_after = [
        profile
        for profile in imported_codex
        if _mapping_payload(profile.get("prompt_freshness")).get("status")
        == CODEX_AUTOMATION_SYNC_STATUS_CUSTOM
    ]
    historical_after = [
        profile
        for profile in imported_codex
        if str(profile.get("profile_key") or "") not in source_keys
    ]
    created_keys = sorted(source_keys - before_keys)
    refreshed_keys = sorted(set(refreshable_before) - {str(profile.get("profile_key") or "") for profile in stale_after})
    return {
        "repo_id": int(repo.id),
        "source_count": len(source_keys),
        "imported_count": len(imported_codex),
        "current_count": len(current_codex),
        "created_count": len(created_keys),
        "refreshed_count": len(refreshed_keys),
        "custom_count": len(custom_after),
        "historical_count": len(historical_after),
        "stale_count": len(stale_after),
        "created_profile_keys": created_keys,
        "refreshed_profile_keys": refreshed_keys,
        "custom_profile_keys": sorted(str(profile.get("profile_key") or "") for profile in custom_after),
        "historical_profile_keys": sorted(str(profile.get("profile_key") or "") for profile in historical_after),
        "stale_profile_keys": sorted(str(profile.get("profile_key") or "") for profile in stale_after),
        "agents": agents,
        "message": (
            f"Synced {len(current_codex)} current Codex profile(s): "
            f"{len(created_keys)} created, {len(refreshed_keys)} refreshed, "
            f"{len(custom_after)} custom override(s) preserved, {len(stale_after)} still stale."
        ),
    }


def list_agent_profiles(
    db: Session,
    *,
    repo_id: int | None = None,
    user_id: int | None = None,
    bootstrap: bool = True,
) -> list[dict[str, Any]]:
    if repo_id is not None:
        repo = _resolve_repo_for_run(db, repo_id, user_id=user_id)
        if repo is not None:
            canonical_repo = _canonical_repo_for_autopilot(db, repo, user_id=user_id)
            repo_id = int(canonical_repo.id)
    if repo_id is not None and bootstrap:
        bootstrap_agent_profiles(db, repo_id=repo_id, user_id=user_id)
    codex_definitions: dict[str, dict[str, Any]] = {}
    if repo_id is not None:
        repo = db.get(CodeRepo, int(repo_id))
        if repo is not None:
            codex_definitions = _codex_automation_definitions_by_key(repo)
    q = _agent_profile_query(db, user_id=user_id, repo_id=repo_id)
    profiles = q.order_by(
        ProjectAutonomyAgentProfile.repo_id.asc(),
        ProjectAutonomyAgentProfile.tier.asc(),
        ProjectAutonomyAgentProfile.profile_key.asc(),
    ).all()
    return [
        _agent_profile_payload(
            db,
            profile,
            codex_definition=codex_definitions.get(str(profile.profile_key)),
        )
        for profile in profiles
    ]


def _readiness_check(
    key: str,
    status: str,
    title: str,
    detail: str,
    *,
    count: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "key": key,
        "status": status,
        "title": title,
        "detail": detail,
    }
    if count is not None:
        payload["count"] = int(count)
    return payload


def _mapping_payload(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _codex_schedule_counts(profiles: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    total = 0
    source_active = 0
    source_paused = 0
    source_active_enabled = 0
    source_active_disabled = 0
    source_active_always_on = 0
    source_active_scheduled = 0
    for profile in profiles:
        prompt_setting = _mapping_payload(profile.get("prompt_setting"))
        if prompt_setting.get("source") != CODEX_AUTOMATION_SOURCE:
            continue
        total += 1
        schedule = _mapping_payload(profile.get("schedule"))
        source_status = str(schedule.get("source_status") or "").strip().upper()
        if source_status == "ACTIVE":
            source_active += 1
            if profile.get("schedule_enabled") is True:
                source_active_enabled += 1
                if _agent_runtime_is_always_on(schedule):
                    source_active_always_on += 1
                else:
                    source_active_scheduled += 1
            else:
                source_active_disabled += 1
        elif source_status:
            source_paused += 1
    return {
        "total": total,
        "source_active": source_active,
        "source_paused": source_paused,
        "source_active_enabled": source_active_enabled,
        "source_active_disabled": source_active_disabled,
        "source_active_always_on": source_active_always_on,
        "source_active_scheduled": source_active_scheduled,
    }


def _agent_codex_bench_payload(
    *,
    matching_count: int,
    imported_codex: list[Mapping[str, Any]],
    current_imported_codex: list[Mapping[str, Any]],
    historical_codex: list[Mapping[str, Any]],
    stale_codex: list[Mapping[str, Any]],
    custom_codex: list[Mapping[str, Any]],
    missing_profile_keys: list[str],
    codex_schedule_counts: Mapping[str, Any],
    codex_contract_coverage: Mapping[str, Any],
    codex_alignment: Mapping[str, Any],
) -> dict[str, Any]:
    source_active = int(codex_schedule_counts.get("source_active") or 0)
    source_active_enabled = int(codex_schedule_counts.get("source_active_enabled") or 0)
    source_active_disabled = int(codex_schedule_counts.get("source_active_disabled") or 0)
    source_active_always_on = int(codex_schedule_counts.get("source_active_always_on") or 0)
    source_active_scheduled = int(codex_schedule_counts.get("source_active_scheduled") or 0)
    contract_missing = int(codex_contract_coverage.get("missing_workspace_count") or 0)
    missing_count = len(missing_profile_keys)
    stale_count = len(stale_codex)
    custom_count = len(custom_codex)
    alignment_score = int(codex_alignment.get("score") or 0)
    alignment_status = str(codex_alignment.get("status") or AGENT_OS_READINESS_CHECK_PASSED)

    if matching_count <= 0:
        status = AGENT_OS_READINESS_CHECK_PASSED
        detail = "No local Codex automations match this repo yet, so the CHILI bench is running on generated repo agents only."
        next_action = AGENT_CODEX_BENCH_ACTION_KEEP_MONITORING
        next_action_label = "Keep monitoring"
        next_action_detail = "Create or point Codex automations at this repo when you want CHILI to mirror them."
    elif missing_count:
        status = AGENT_OS_READINESS_CHECK_WARNING
        detail = f"CHILI mirrors {len(current_imported_codex)} of {matching_count} current Codex automation(s); {missing_count} still need profiles."
        next_action = AGENT_CODEX_BENCH_ACTION_SYNC
        next_action_label = "Sync Codex prompts"
        next_action_detail = "Bootstrap or resync agent profiles from the local Codex automation files."
    elif stale_count:
        status = AGENT_OS_READINESS_CHECK_WARNING
        detail = f"CHILI has all {matching_count} Codex automation profile(s), but {stale_count} prompt snapshot(s) are stale or missing their source file."
        next_action = AGENT_CODEX_BENCH_ACTION_SYNC
        next_action_label = "Sync Codex prompts"
        next_action_detail = "Refresh stale prompt snapshots before trusting scheduled agent behavior."
    elif custom_count:
        status = AGENT_OS_READINESS_CHECK_WARNING
        detail = f"CHILI mirrors all {matching_count} Codex automation(s), with {custom_count} custom prompt override(s) preserved."
        next_action = AGENT_CODEX_BENCH_ACTION_REVIEW_CUSTOM
        next_action_label = "Review custom prompts"
        next_action_detail = "Custom overrides are safe, but they should be intentionally reviewed against the source automation prompt."
    elif contract_missing:
        status = AGENT_OS_READINESS_CHECK_WARNING
        detail = f"CHILI mirrors all {matching_count} Codex automation(s), but {contract_missing} imported agent(s) are missing workspace contracts."
        next_action = AGENT_CODEX_BENCH_ACTION_REVIEW_CONTRACTS
        next_action_label = "Review contracts"
        next_action_detail = "Workspace, mailbox, command, and safety evidence make long-running agents auditable."
    elif source_active_disabled:
        status = AGENT_OS_READINESS_CHECK_WARNING
        detail = f"CHILI mirrors all {matching_count} Codex automation(s); {source_active_disabled} source-active automation(s) are still paused in CHILI."
        next_action = AGENT_CODEX_BENCH_ACTION_ENABLE_ACTIVE
        next_action_label = "Enable source-active agents"
        next_action_detail = "Use the always-on mirror when you want CHILI to keep source-active Codex agents queue-driven and plan-only."
    elif alignment_status != AGENT_OS_READINESS_CHECK_PASSED:
        status = AGENT_OS_READINESS_CHECK_WARNING
        detail = str(
            codex_alignment.get("detail")
            or "Local-vs-Codex alignment has a gap that needs review."
        )
        next_action = AGENT_CODEX_BENCH_ACTION_REVIEW_ALIGNMENT
        next_action_label = "Review alignment"
        next_action_detail = "Inspect the alignment card for the weakest clean-room parity dimension."
    else:
        status = AGENT_OS_READINESS_CHECK_PASSED
        detail = (
            f"CHILI mirrors all {matching_count} current Codex automation(s): "
            f"{source_active_always_on} always-on, {source_active_scheduled} scheduled, "
            f"{source_active_enabled} of {source_active} source-active enabled."
        )
        next_action = AGENT_CODEX_BENCH_ACTION_KEEP_MONITORING
        next_action_label = "Keep monitoring"
        next_action_detail = "The local Codex bench is mirrored and guarded by CHILI safety gates."

    return {
        "status": status,
        "detail": detail,
        "matching_count": matching_count,
        "imported_count": len(imported_codex),
        "current_imported_count": len(current_imported_codex),
        "historical_count": len(historical_codex),
        "missing_count": missing_count,
        "stale_count": stale_count,
        "custom_count": custom_count,
        "source_active_count": source_active,
        "source_active_enabled_count": source_active_enabled,
        "source_active_disabled_count": source_active_disabled,
        "source_active_always_on_count": source_active_always_on,
        "source_active_scheduled_count": source_active_scheduled,
        "contract_workspace_count": int(codex_contract_coverage.get("workspace_count") or 0),
        "contract_missing_workspace_count": contract_missing,
        "d_drive_aligned_count": int(codex_contract_coverage.get("d_drive_aligned_count") or 0),
        "alignment_score": alignment_score,
        "alignment_status": alignment_status,
        "missing_profile_keys": missing_profile_keys,
        "stale_profile_keys": [
            str(profile.get("profile_key") or "")
            for profile in stale_codex
            if str(profile.get("profile_key") or "").strip()
        ],
        "custom_override_keys": [
            str(profile.get("profile_key") or "")
            for profile in custom_codex
            if str(profile.get("profile_key") or "").strip()
        ],
        "next_action": next_action,
        "next_action_label": next_action_label,
        "next_action_detail": next_action_detail,
    }


def _codex_automation_profile_matrix(
    definitions: Iterable[Mapping[str, Any]],
    profiles: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    profiles_by_key = {
        str(profile.get("profile_key") or ""): profile
        for profile in profiles
        if str(profile.get("profile_key") or "").strip()
    }
    rows: list[dict[str, Any]] = []
    for definition in sorted(
        definitions,
        key=lambda item: str(item.get("name") or item.get("profile_key") or "").lower(),
    ):
        key = str(definition.get("profile_key") or "").strip()
        if not key:
            continue
        profile = profiles_by_key.get(key)
        source = _mapping_payload(definition.get("codex_automation"))
        source_schedule = _mapping_payload(definition.get("schedule"))
        profile_schedule = _mapping_payload(profile.get("schedule")) if profile else {}
        freshness = (
            _mapping_payload(profile.get("prompt_freshness"))
            if profile
            else {
                "status": CODEX_AUTOMATION_SYNC_STATUS_MISSING_PROFILE,
                "reasons": ["missing_chili_profile"],
            }
        )
        permissions = _mapping_payload(profile.get("permissions")) if profile else {}
        prompt_preview = truncate_text(
            str(definition.get("prompt") or ""),
            CODEX_AUTOMATION_PROMPT_PREVIEW_LIMIT,
        )[0]
        operating_contract = _mapping_payload(
            definition.get("operating_contract")
        ) or _codex_operating_contract(
            str(definition.get("prompt") or ""),
            source,
        )
        rows.append(
            {
                "profile_key": key,
                "name": definition.get("name") or source.get("name") or key,
                "role": (profile or definition).get("role"),
                "tier": (profile or definition).get("tier"),
                "kind": source.get("kind"),
                "source_status": source.get("status") or source_schedule.get("source_status"),
                "source_rrule": source.get("rrule") or source_schedule.get("rrule"),
                "chili_status": profile.get("status") if profile else None,
                "chili_schedule_enabled": bool(profile.get("schedule_enabled")) if profile else False,
                "chili_rrule": profile_schedule.get("rrule"),
                "prompt_freshness_status": freshness.get("status"),
                "prompt_freshness_reasons": [
                    str(reason)
                    for reason in (
                        freshness.get("reasons")
                        if isinstance(freshness.get("reasons"), list)
                        else []
                    )
                ],
                "can_patch": bool(permissions.get(AGENT_PERMISSION_WORKTREE)),
                "can_merge": bool(permissions.get(AGENT_PERMISSION_MERGE)),
                "prompt_preview": prompt_preview,
                "operating_contract": operating_contract,
                "operating_state": _mapping_payload(profile.get("operating_state")) if profile else {},
            }
        )
    return rows


def _codex_operating_contract_coverage(
    profiles: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    profile_list = list(profiles)
    total = len(profile_list)
    missing_workspace: list[str] = []
    d_drive_aligned = 0
    inferred_workspace = 0
    with_commands = 0
    with_safety_boundaries = 0
    mailbox_protocol = 0
    run_lock = 0
    out_report = 0
    pr_review_flow = 0
    for profile in profile_list:
        key = str(profile.get("profile_key") or profile.get("name") or "codex_agent")
        contract = _mapping_payload(profile.get("operating_contract"))
        if str(contract.get("workspace") or "").strip():
            pass
        else:
            missing_workspace.append(key)
        if contract.get("d_drive_aligned") is True:
            d_drive_aligned += 1
        if contract.get("workspace_inferred_from_repo") is True:
            inferred_workspace += 1
        if contract.get("uses_mailbox_protocol") is True:
            mailbox_protocol += 1
        if contract.get("uses_run_lock") is True:
            run_lock += 1
        if contract.get("requires_out_report") is True:
            out_report += 1
        if contract.get("uses_pr_review_flow") is True:
            pr_review_flow += 1
        if contract.get("key_commands"):
            with_commands += 1
        if contract.get("safety_boundaries"):
            with_safety_boundaries += 1
    workspace_count = total - len(missing_workspace)
    return {
        "total": total,
        "workspace_count": workspace_count,
        "missing_workspace_count": len(missing_workspace),
        "missing_workspace_keys": sorted(missing_workspace),
        "d_drive_aligned_count": d_drive_aligned,
        "inferred_workspace_count": inferred_workspace,
        "key_command_profile_count": with_commands,
        "safety_boundary_profile_count": with_safety_boundaries,
        "mailbox_protocol_count": mailbox_protocol,
        "run_lock_count": run_lock,
        "out_report_count": out_report,
        "pr_review_flow_count": pr_review_flow,
    }


def _codex_alignment_dimension(
    key: str,
    label: str,
    status: str,
    detail: str,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "status": status,
        "detail": detail,
    }


def _agent_codex_alignment_scorecard(
    *,
    matching_count: int,
    imported_codex: list[Mapping[str, Any]],
    missing_profile_keys: list[str],
    stale_codex: list[Mapping[str, Any]],
    codex_contract_coverage: Mapping[str, Any],
    quality_scorecard: Mapping[str, Any],
    runtime_queue: Mapping[str, Any],
    operator_inbox: Mapping[str, Any],
    local_model: Mapping[str, Any],
) -> dict[str, Any]:
    if matching_count <= 0:
        return {
            "status": AGENT_OS_READINESS_CHECK_PASSED,
            "score": 100,
            "detail": "No local Codex reference automations were found for this repo, so there is no Codex contract drift to compare.",
            "reference_count": 0,
            "imported_count": 0,
            "dimensions": [],
            "gaps": [],
        }

    imported_count = len(imported_codex)
    missing_count = len(missing_profile_keys)
    extra_imported_count = max(imported_count - max(matching_count - missing_count, 0), 0)
    contract_total = int(codex_contract_coverage.get("total") or 0)
    missing_workspace = int(codex_contract_coverage.get("missing_workspace_count") or 0)
    command_count = int(codex_contract_coverage.get("key_command_profile_count") or 0)
    safety_count = int(codex_contract_coverage.get("safety_boundary_profile_count") or 0)
    protocol_count = sum(
        int(codex_contract_coverage.get(key) or 0)
        for key in AGENT_CODEX_ALIGNMENT_PROTOCOL_COVERAGE_KEYS
    )
    mutation_enabled = [
        str(profile.get("profile_key") or profile.get("name") or "codex_agent")
        for profile in imported_codex
        if bool(_mapping_payload(profile.get("permissions")).get(AGENT_PERMISSION_WORKTREE))
        or bool(_mapping_payload(profile.get("permissions")).get(AGENT_PERMISSION_MERGE))
    ]
    inbox_actions = int(operator_inbox.get("total_action_count") or 0)
    runtime_status = str(runtime_queue.get("status") or "")
    governance_status = str(quality_scorecard.get("status") or "")
    dimensions = [
        _codex_alignment_dimension(
            AGENT_CODEX_ALIGNMENT_DIMENSION_IMPORT,
            "Profile import",
            (
                AGENT_OS_READINESS_CHECK_PASSED
                if missing_count == 0
                else AGENT_OS_READINESS_CHECK_WARNING
            ),
            (
                (
                    f"All {matching_count} current local Codex automation(s) have CHILI profiles; "
                    f"{extra_imported_count} historical imported profile(s) are retained for audit."
                )
                if missing_count == 0
                else f"{missing_count} current local Codex automation profile(s) are missing in CHILI."
            ),
        ),
        _codex_alignment_dimension(
            AGENT_CODEX_ALIGNMENT_DIMENSION_FRESHNESS,
            "Prompt freshness",
            (
                AGENT_OS_READINESS_CHECK_PASSED
                if not stale_codex
                else AGENT_OS_READINESS_CHECK_WARNING
            ),
            (
                "Imported Codex prompt snapshots are current."
                if not stale_codex
                else f"{len(stale_codex)} imported Codex prompt snapshot(s) are stale or missing source files."
            ),
        ),
        _codex_alignment_dimension(
            AGENT_CODEX_ALIGNMENT_DIMENSION_CONTRACTS,
            "Operating contracts",
            (
                AGENT_OS_READINESS_CHECK_PASSED
                if contract_total > 0 and missing_workspace == 0 and command_count > 0
                else AGENT_OS_READINESS_CHECK_WARNING
            ),
            (
                f"{contract_total} imported Codex agent(s) expose workspace contracts and {command_count} command evidence set(s)."
                if contract_total > 0 and missing_workspace == 0 and command_count > 0
                else "Some imported Codex agents are missing workspace or command evidence for clean-room parity."
            ),
        ),
        _codex_alignment_dimension(
            AGENT_CODEX_ALIGNMENT_DIMENSION_PROTOCOL,
            "Operating protocol",
            (
                AGENT_OS_READINESS_CHECK_PASSED
                if protocol_count > 0 and safety_count > 0
                else AGENT_OS_READINESS_CHECK_WARNING
            ),
            (
                "Codex-style mailbox, lock, report, PR, or safety-boundary signals are present."
                if protocol_count > 0 and safety_count > 0
                else "Imported Codex prompts lack enough explicit mailbox, lock, report, PR, or safety-boundary signals to audit behavior."
            ),
        ),
        _codex_alignment_dimension(
            AGENT_CODEX_ALIGNMENT_DIMENSION_SAFETY,
            "Mutation safety",
            (
                AGENT_OS_READINESS_CHECK_PASSED
                if not mutation_enabled
                else AGENT_OS_READINESS_CHECK_WARNING
            ),
            (
                "Imported Codex-profile agents remain observe/plan-first; patch and merge permissions are off."
                if not mutation_enabled
                else f"{len(mutation_enabled)} imported Codex-profile agent(s) can patch or merge."
            ),
        ),
        _codex_alignment_dimension(
            AGENT_CODEX_ALIGNMENT_DIMENSION_GOVERNANCE,
            "Quality governance",
            (
                AGENT_OS_READINESS_CHECK_PASSED
                if governance_status == AGENT_OS_READINESS_CHECK_PASSED
                else AGENT_OS_READINESS_CHECK_WARNING
            ),
            str(quality_scorecard.get("detail") or "Quality scorecard has not reported yet."),
        ),
        _codex_alignment_dimension(
            AGENT_CODEX_ALIGNMENT_DIMENSION_RUNTIME,
            "Runtime flow",
            (
                AGENT_OS_READINESS_CHECK_PASSED
                if runtime_status == AGENT_OS_READINESS_CHECK_PASSED and inbox_actions == 0
                else AGENT_OS_READINESS_CHECK_WARNING
            ),
            (
                "Runtime queue and operator inbox are clear."
                if runtime_status == AGENT_OS_READINESS_CHECK_PASSED and inbox_actions == 0
                else f"Runtime status is {runtime_status or 'unknown'} with {inbox_actions} operator inbox action(s)."
            ),
        ),
        _codex_alignment_dimension(
            AGENT_CODEX_ALIGNMENT_DIMENSION_MODEL,
            "Local model bridge",
            (
                AGENT_OS_READINESS_CHECK_PASSED
                if bool(local_model.get("coding_ready"))
                else AGENT_OS_READINESS_CHECK_WARNING
            ),
            (
                f"Local model {local_model.get('model') or 'unknown'} is coder-ready."
                if bool(local_model.get("coding_ready"))
                else str(local_model.get("detail") or "Local model is available but not coder-tuned.")
            ),
        ),
    ]
    score_points = 0.0
    for dimension in dimensions:
        status = str(dimension.get("status") or "")
        if status == AGENT_OS_READINESS_CHECK_PASSED:
            score_points += AGENT_CODEX_ALIGNMENT_PASS_POINTS
        elif status == AGENT_OS_READINESS_CHECK_WARNING:
            score_points += AGENT_CODEX_ALIGNMENT_WARNING_POINTS
        else:
            score_points += AGENT_CODEX_ALIGNMENT_FAIL_POINTS
    score = round((score_points / max(len(dimensions), 1)) * 100)
    gaps = [
        dimension
        for dimension in dimensions
        if dimension.get("status") != AGENT_OS_READINESS_CHECK_PASSED
    ]
    status = (
        AGENT_OS_READINESS_CHECK_PASSED
        if score >= AGENT_CODEX_ALIGNMENT_PASSING_SCORE
        and not any(dimension.get("status") == AGENT_OS_READINESS_CHECK_FAILED for dimension in dimensions)
        else AGENT_OS_READINESS_CHECK_WARNING
    )
    detail = (
        f"Local Agent OS matches the Codex automation contract with {score}% alignment across {len(dimensions)} dimension(s)."
        if not gaps
        else (
            f"Local-vs-Codex alignment is {score}%; watch "
            + ", ".join(str(gap.get("label") or gap.get("key")) for gap in gaps[:AGENT_OS_QUALITY_PROBLEM_PREVIEW_LIMIT])
            + "."
        )
    )
    return {
        "status": status,
        "score": score,
        "detail": detail,
        "reference_count": matching_count,
        "imported_count": imported_count,
        "missing_profile_keys": missing_profile_keys,
        "extra_imported_count": extra_imported_count,
        "dimension_count": len(dimensions),
        "dimensions": dimensions,
        "gaps": gaps[:AGENT_OS_QUALITY_PROBLEM_PREVIEW_LIMIT],
    }


def _agent_os_capability_item(
    key: str,
    label: str,
    status: str,
    detail: str,
    *,
    next_action: str = AGENT_OS_CAPABILITY_ACTION_KEEP_MONITORING,
    next_action_label: str = "Keep monitoring",
    count: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "key": key,
        "label": label,
        "status": status,
        "detail": detail,
        "next_action": next_action,
        "next_action_label": next_action_label,
    }
    if count is not None:
        payload["count"] = int(count)
    return payload


def _agent_os_capability_audit(
    *,
    repo_ready: bool,
    runtime_path: Path | None,
    profiles: list[Mapping[str, Any]],
    agent_teams: list[Mapping[str, Any]],
    matching_codex_count: int,
    imported_codex: list[Mapping[str, Any]],
    current_imported_codex: list[Mapping[str, Any]],
    historical_codex: list[Mapping[str, Any]],
    stale_codex: list[Mapping[str, Any]],
    custom_codex: list[Mapping[str, Any]],
    missing_profile_keys: list[str],
    codex_contract_coverage: Mapping[str, Any],
    codex_schedule_counts: Mapping[str, Any],
    quality_monitor: Mapping[str, Any],
    quality_scorecard: Mapping[str, Any],
    runtime_queue: Mapping[str, Any],
    operator_inbox: Mapping[str, Any],
    local_model: Mapping[str, Any],
    worktree_enabled_count: int,
    merge_enabled_count: int,
) -> dict[str, Any]:
    capabilities: list[dict[str, Any]] = []
    capabilities.append(
        _agent_os_capability_item(
            AGENT_OS_CAPABILITY_REPO_RUNTIME,
            "D-drive repo runtime",
            AGENT_OS_READINESS_CHECK_PASSED if repo_ready else AGENT_OS_READINESS_CHECK_FAILED,
            (
                f"Autopilot resolves this repo to {runtime_path}."
                if repo_ready
                else "Autopilot cannot resolve the selected repo path from this runtime."
            ),
            next_action=(
                AGENT_OS_CAPABILITY_ACTION_KEEP_MONITORING
                if repo_ready
                else AGENT_OS_CAPABILITY_ACTION_FIX_REPO
            ),
            next_action_label="Keep monitoring" if repo_ready else "Fix repo path",
        )
    )

    hierarchy_ready = bool(profiles and agent_teams)
    capabilities.append(
        _agent_os_capability_item(
            AGENT_OS_CAPABILITY_AGENT_HIERARCHY,
            "Agent hierarchy",
            (
                AGENT_OS_READINESS_CHECK_PASSED
                if hierarchy_ready
                else AGENT_OS_READINESS_CHECK_FAILED
                if not profiles
                else AGENT_OS_READINESS_CHECK_WARNING
            ),
            (
                f"{len(profiles)} profile(s) and {len(agent_teams)} supervision team(s) are available."
                if hierarchy_ready
                else "Agent profiles exist, but macro supervision teams need attention."
                if profiles
                else "No repo agent profiles are available yet."
            ),
            next_action=(
                AGENT_OS_CAPABILITY_ACTION_KEEP_MONITORING
                if hierarchy_ready
                else AGENT_OS_CAPABILITY_ACTION_BOOTSTRAP
            ),
            next_action_label="Keep monitoring" if hierarchy_ready else "Bootstrap agents",
            count=len(profiles),
        )
    )

    codex_missing = len(missing_profile_keys)
    codex_stale = len(stale_codex)
    codex_custom = len(custom_codex)
    if matching_codex_count <= 0:
        codex_status = AGENT_OS_READINESS_CHECK_PASSED
        codex_detail = "No local Codex automations point at this repo yet; generated CHILI repo agents are the source of truth."
        codex_action = AGENT_OS_CAPABILITY_ACTION_KEEP_MONITORING
        codex_action_label = "Keep monitoring"
    elif codex_missing or codex_stale:
        codex_status = AGENT_OS_READINESS_CHECK_WARNING
        codex_detail = (
            f"{len(current_imported_codex)}/{matching_codex_count} current Codex automation(s) are mirrored; "
            f"{codex_missing} missing and {codex_stale} stale."
        )
        codex_action = AGENT_OS_CAPABILITY_ACTION_SYNC_CODEX
        codex_action_label = "Sync Codex prompts"
    elif codex_custom:
        codex_status = AGENT_OS_READINESS_CHECK_WARNING
        codex_detail = f"All {matching_codex_count} current Codex automation(s) are mirrored, with {codex_custom} custom prompt override(s) preserved."
        codex_action = AGENT_CODEX_BENCH_ACTION_REVIEW_CUSTOM
        codex_action_label = "Review custom prompts"
    else:
        codex_status = AGENT_OS_READINESS_CHECK_PASSED
        codex_detail = (
            f"All {matching_codex_count} current Codex automation(s) are mirrored; "
            f"{len(historical_codex)} historical profile(s) are retained for audit."
        )
        codex_action = AGENT_OS_CAPABILITY_ACTION_KEEP_MONITORING
        codex_action_label = "Keep monitoring"
    capabilities.append(
        _agent_os_capability_item(
            AGENT_OS_CAPABILITY_CODEX_MIRROR,
            "Codex prompt mirror",
            codex_status,
            codex_detail,
            next_action=codex_action,
            next_action_label=codex_action_label,
            count=len(current_imported_codex),
        )
    )

    imported_count = len(imported_codex)
    missing_contracts = int(codex_contract_coverage.get("missing_workspace_count") or 0)
    command_contracts = int(codex_contract_coverage.get("key_command_profile_count") or 0)
    contract_status = (
        AGENT_OS_READINESS_CHECK_PASSED
        if imported_count == 0 or (missing_contracts == 0 and command_contracts > 0)
        else AGENT_OS_READINESS_CHECK_WARNING
    )
    capabilities.append(
        _agent_os_capability_item(
            AGENT_OS_CAPABILITY_OPERATING_CONTRACTS,
            "Operating contracts",
            contract_status,
            (
                "Imported Codex agents expose workspace and command evidence for audit."
                if contract_status == AGENT_OS_READINESS_CHECK_PASSED and imported_count > 0
                else "No imported Codex agents need operating contracts yet."
                if imported_count == 0
                else f"{missing_contracts} imported agent(s) lack workspace contracts or command evidence."
            ),
            next_action=(
                AGENT_OS_CAPABILITY_ACTION_KEEP_MONITORING
                if contract_status == AGENT_OS_READINESS_CHECK_PASSED
                else AGENT_OS_CAPABILITY_ACTION_REVIEW_CONTRACTS
            ),
            next_action_label=(
                "Keep monitoring"
                if contract_status == AGENT_OS_READINESS_CHECK_PASSED
                else "Review contracts"
            ),
            count=imported_count,
        )
    )

    mutation_safe = worktree_enabled_count == 0 and merge_enabled_count == 0
    capabilities.append(
        _agent_os_capability_item(
            AGENT_OS_CAPABILITY_SAFE_DEFAULTS,
            "Mutation safety",
            AGENT_OS_READINESS_CHECK_PASSED if mutation_safe else AGENT_OS_READINESS_CHECK_WARNING,
            (
                "All generated and imported agents are observe/research/plan-first by default."
                if mutation_safe
                else f"{worktree_enabled_count} agent(s) can patch and {merge_enabled_count} can merge; review permissions."
            ),
            next_action=(
                AGENT_OS_CAPABILITY_ACTION_KEEP_MONITORING
                if mutation_safe
                else AGENT_OS_CAPABILITY_ACTION_REVIEW_PERMISSIONS
            ),
            next_action_label="Keep monitoring" if mutation_safe else "Review permissions",
        )
    )

    runtime_status = str(runtime_queue.get("status") or AGENT_OS_READINESS_CHECK_WARNING)
    capabilities.append(
        _agent_os_capability_item(
            AGENT_OS_CAPABILITY_RUNTIME_QUEUE,
            "Runtime queue",
            runtime_status,
            str(runtime_queue.get("detail") or "Runtime queue has not reported yet."),
            next_action=(
                AGENT_OS_CAPABILITY_ACTION_KEEP_MONITORING
                if runtime_status == AGENT_OS_READINESS_CHECK_PASSED
                else AGENT_OS_CAPABILITY_ACTION_DRAIN_QUEUE
            ),
            next_action_label=(
                "Keep monitoring"
                if runtime_status == AGENT_OS_READINESS_CHECK_PASSED
                else "Drain queue"
            ),
            count=int(runtime_queue.get("open_count") or 0),
        )
    )

    source_active = int(codex_schedule_counts.get("source_active") or 0)
    always_on_count = int(codex_schedule_counts.get("source_active_always_on") or 0)
    active_enabled = int(codex_schedule_counts.get("source_active_enabled") or 0)
    always_on_ready = source_active == 0 or always_on_count > 0 or active_enabled > 0
    capabilities.append(
        _agent_os_capability_item(
            AGENT_OS_CAPABILITY_ALWAYS_ON,
            "Always-on work loop",
            AGENT_OS_READINESS_CHECK_PASSED if always_on_ready else AGENT_OS_READINESS_CHECK_WARNING,
            (
                f"{always_on_count} always-on and {active_enabled - always_on_count} scheduled source-active agent(s) are enabled."
                if source_active > 0 and always_on_ready
                else "Source-active Codex agents exist, but none are enabled in CHILI yet."
                if source_active > 0
                else "No source-active Codex agents need always-on mirroring yet."
            ),
            next_action=(
                AGENT_OS_CAPABILITY_ACTION_KEEP_MONITORING
                if always_on_ready
                else AGENT_OS_CAPABILITY_ACTION_ENABLE_ALWAYS_ON
            ),
            next_action_label="Keep monitoring" if always_on_ready else "Enable always-on",
            count=always_on_count,
        )
    )

    quality_status = str(quality_monitor.get("status") or AGENT_OS_READINESS_CHECK_WARNING)
    capabilities.append(
        _agent_os_capability_item(
            AGENT_OS_CAPABILITY_ARCHITECT_QUALITY,
            "Architect quality",
            quality_status,
            str(
                quality_monitor.get("detail")
                or "Architect review and scheduled quality signals have not reported yet."
            ),
            next_action=(
                AGENT_OS_CAPABILITY_ACTION_KEEP_MONITORING
                if quality_status == AGENT_OS_READINESS_CHECK_PASSED
                else AGENT_OS_CAPABILITY_ACTION_REVIEW_QUALITY
            ),
            next_action_label=(
                "Keep monitoring"
                if quality_status == AGENT_OS_READINESS_CHECK_PASSED
                else "Review quality"
            ),
            count=int(quality_scorecard.get("recent_run_count") or 0),
        )
    )

    local_model_ready = bool(local_model.get("coding_ready"))
    capabilities.append(
        _agent_os_capability_item(
            AGENT_OS_CAPABILITY_LOCAL_MODEL,
            "Local model bridge",
            AGENT_OS_READINESS_CHECK_PASSED if local_model_ready else AGENT_OS_READINESS_CHECK_WARNING,
            str(local_model.get("detail") or "Local model readiness has not reported yet."),
            next_action=(
                AGENT_OS_CAPABILITY_ACTION_KEEP_MONITORING
                if local_model_ready
                else AGENT_OS_CAPABILITY_ACTION_INSTALL_MODEL
            ),
            next_action_label="Keep monitoring" if local_model_ready else "Install coder model",
            count=len(local_model.get("installed_models") or []),
        )
    )

    inbox_status = str(operator_inbox.get("status") or AGENT_OS_READINESS_CHECK_WARNING)
    capabilities.append(
        _agent_os_capability_item(
            AGENT_OS_CAPABILITY_OPERATOR_LOOP,
            "Operator loop",
            inbox_status,
            str(operator_inbox.get("detail") or "Operator inbox has not reported yet."),
            next_action=(
                AGENT_OS_CAPABILITY_ACTION_KEEP_MONITORING
                if inbox_status == AGENT_OS_READINESS_CHECK_PASSED
                else AGENT_OS_CAPABILITY_ACTION_ANSWER_OPERATOR
            ),
            next_action_label=(
                "Keep monitoring"
                if inbox_status == AGENT_OS_READINESS_CHECK_PASSED
                else "Answer operator prompt"
            ),
            count=int(operator_inbox.get("total_action_count") or 0),
        )
    )

    validation = _mapping_payload(quality_scorecard.get("validation"))
    validation_total = int(validation.get("total") or 0)
    validation_failed = int(validation.get("failed") or 0)
    validation_status = (
        AGENT_OS_READINESS_CHECK_WARNING
        if validation_total > 0 and validation_failed > 0
        else AGENT_OS_READINESS_CHECK_PASSED
    )
    capabilities.append(
        _agent_os_capability_item(
            AGENT_OS_CAPABILITY_VALIDATION_SAFETY,
            "Validation and merge safety",
            validation_status,
            (
                f"{int(validation.get('passed') or 0)}/{validation_total} recent validation set(s) passed."
                if validation_total
                else "Validation and merge gates are installed; recent runs have not produced validation evidence yet."
            ),
            next_action=(
                AGENT_OS_CAPABILITY_ACTION_KEEP_MONITORING
                if validation_status == AGENT_OS_READINESS_CHECK_PASSED
                else AGENT_OS_CAPABILITY_ACTION_RUN_VALIDATION
            ),
            next_action_label=(
                "Keep monitoring"
                if validation_status == AGENT_OS_READINESS_CHECK_PASSED
                else "Run validation"
            ),
            count=validation_total,
        )
    )

    failed = sum(1 for item in capabilities if item["status"] == AGENT_OS_READINESS_CHECK_FAILED)
    warnings = sum(1 for item in capabilities if item["status"] == AGENT_OS_READINESS_CHECK_WARNING)
    passed = len(capabilities) - failed - warnings
    score_points = 0.0
    for item in capabilities:
        status = str(item.get("status") or "")
        if status == AGENT_OS_READINESS_CHECK_PASSED:
            score_points += AGENT_CODEX_ALIGNMENT_PASS_POINTS
        elif status == AGENT_OS_READINESS_CHECK_WARNING:
            score_points += AGENT_CODEX_ALIGNMENT_WARNING_POINTS
        else:
            score_points += AGENT_CODEX_ALIGNMENT_FAIL_POINTS
    score = round((score_points / max(len(capabilities), 1)) * 100)
    gaps = [
        item
        for item in capabilities
        if item["status"] != AGENT_OS_READINESS_CHECK_PASSED
    ]
    if failed:
        status = AGENT_OS_READINESS_CHECK_FAILED
    elif warnings:
        status = AGENT_OS_READINESS_CHECK_WARNING
    else:
        status = AGENT_OS_READINESS_CHECK_PASSED
    if gaps:
        detail = (
            f"{passed}/{len(capabilities)} Agent OS capability area(s) are clean; "
            + ", ".join(str(gap["label"]) for gap in gaps[:AGENT_OS_QUALITY_PROBLEM_PREVIEW_LIMIT])
            + " need attention."
        )
        first_gap = gaps[0]
        next_action = str(first_gap.get("next_action") or AGENT_OS_CAPABILITY_ACTION_KEEP_MONITORING)
        next_action_label = str(first_gap.get("next_action_label") or "Review gap")
        next_action_detail = str(first_gap.get("detail") or "")
    else:
        detail = f"All {len(capabilities)} Agent OS capability area(s) are installed, live, and guarded."
        next_action = AGENT_OS_CAPABILITY_ACTION_KEEP_MONITORING
        next_action_label = "Keep monitoring"
        next_action_detail = "The cockpit has no capability gaps to surface right now."
    return {
        "status": status,
        "score": score,
        "detail": detail,
        "passed": passed,
        "warnings": warnings,
        "failed": failed,
        "capability_count": len(capabilities),
        "capabilities": capabilities,
        "gaps": gaps[:AGENT_OS_QUALITY_PROBLEM_PREVIEW_LIMIT],
        "next_action": next_action,
        "next_action_label": next_action_label,
        "next_action_detail": next_action_detail,
    }


def _agent_team_member_payload(profile: Mapping[str, Any]) -> dict[str, Any]:
    permissions = _mapping_payload(profile.get("permissions"))
    schedule = _mapping_payload(profile.get("schedule"))
    return {
        "id": profile.get("id"),
        "profile_key": profile.get("profile_key"),
        "name": profile.get("name"),
        "role": profile.get("role"),
        "tier": profile.get("tier"),
        "status": profile.get("status"),
        "schedule_enabled": bool(profile.get("schedule_enabled")),
        "schedule_status": schedule.get("status"),
        "active_run_count": int(profile.get("active_run_count") or 0),
        "pending_question_count": int(profile.get("pending_question_count") or 0),
        "source": _mapping_payload(profile.get("prompt_setting")).get("source"),
        "can_patch": bool(permissions.get(AGENT_PERMISSION_WORKTREE)),
        "can_merge": bool(permissions.get(AGENT_PERMISSION_MERGE)),
    }


def _agent_team_status(
    children: list[Mapping[str, Any]],
    supervisor: Mapping[str, Any],
) -> str:
    pending_questions = int(supervisor.get("pending_question_count") or 0) + sum(
        int(child.get("pending_question_count") or 0) for child in children
    )
    if pending_questions:
        return "needs_input"
    active_runs = int(supervisor.get("active_run_count") or 0) + sum(
        int(child.get("active_run_count") or 0) for child in children
    )
    if active_runs:
        return "running"
    if bool(supervisor.get("schedule_enabled")) or any(
        bool(child.get("schedule_enabled")) for child in children
    ):
        return "scheduled"
    return "paused"


def _agent_team_topology(profiles: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    profile_list = list(profiles)
    children_by_parent: dict[int, list[Mapping[str, Any]]] = {}
    for profile in profile_list:
        parent_id = profile.get("parent_profile_id")
        try:
            clean_parent_id = int(parent_id)
        except (TypeError, ValueError):
            continue
        children_by_parent.setdefault(clean_parent_id, []).append(profile)

    teams: list[dict[str, Any]] = []
    for profile in sorted(
        profile_list,
        key=lambda item: (
            str(item.get("tier") or "") != AGENT_PROFILE_TIER_MACRO,
            str(item.get("tier") or "") != AGENT_PROFILE_TIER_SPECIALIST,
            str(item.get("name") or item.get("profile_key") or "").lower(),
        ),
    ):
        try:
            profile_id = int(profile.get("id"))
        except (TypeError, ValueError):
            continue
        children = sorted(
            children_by_parent.get(profile_id, []),
            key=lambda item: (
                str(item.get("tier") or ""),
                str(item.get("name") or item.get("profile_key") or "").lower(),
            ),
        )
        if profile.get("tier") != AGENT_PROFILE_TIER_MACRO and not children:
            continue
        pending_questions = int(profile.get("pending_question_count") or 0) + sum(
            int(child.get("pending_question_count") or 0) for child in children
        )
        active_runs = int(profile.get("active_run_count") or 0) + sum(
            int(child.get("active_run_count") or 0) for child in children
        )
        scheduled_children = sum(1 for child in children if child.get("schedule_enabled"))
        codex_children = sum(
            1
            for child in children
            if _mapping_payload(child.get("prompt_setting")).get("source") == CODEX_AUTOMATION_SOURCE
        )
        can_patch = bool(_mapping_payload(profile.get("permissions")).get(AGENT_PERMISSION_WORKTREE)) or any(
            bool(_mapping_payload(child.get("permissions")).get(AGENT_PERMISSION_WORKTREE))
            for child in children
        )
        can_merge = bool(_mapping_payload(profile.get("permissions")).get(AGENT_PERMISSION_MERGE)) or any(
            bool(_mapping_payload(child.get("permissions")).get(AGENT_PERMISSION_MERGE))
            for child in children
        )
        team_status = _agent_team_status(children, profile)
        teams.append(
            {
                "supervisor": _agent_team_member_payload(profile),
                "status": team_status,
                "child_count": len(children),
                "active_run_count": active_runs,
                "pending_question_count": pending_questions,
                "scheduled_child_count": scheduled_children,
                "codex_child_count": codex_children,
                "can_patch": can_patch,
                "can_merge": can_merge,
                "children": [_agent_team_member_payload(child) for child in children],
            }
        )
    return teams


def _local_model_coding_ready(model_name: str | None) -> bool:
    clean = (model_name or "").strip().lower()
    if not clean:
        return False
    return "coder" in clean or clean == "chili-coder:current"


def _local_model_readiness_payload(selection: dict[str, Any]) -> dict[str, Any]:
    model = str(selection.get("model") or "").strip()
    available = bool(selection.get("available"))
    installed = [str(item) for item in selection.get("installed_models") or []]
    skipped = selection.get("skipped_models") if isinstance(selection.get("skipped_models"), dict) else {}
    coding_ready = available and _local_model_coding_ready(model)
    if coding_ready:
        status = AGENT_OS_READINESS_CHECK_PASSED
        detail = f"Using coder-tuned local model {model}."
    elif available:
        status = AGENT_OS_READINESS_CHECK_WARNING
        detail = (
            f"Using general local model {model}; install a coder-tuned model for stronger autonomous coding."
        )
    else:
        status = AGENT_OS_READINESS_CHECK_FAILED
        detail = "No usable local model is available for Autopilot planning."
    return {
        "status": status,
        "model": model or None,
        "available": available,
        "coding_ready": coding_ready,
        "installed_models": installed,
        "skipped_models": skipped,
        "recommendation": selection.get("recommendation") or AGENT_OS_LOCAL_MODEL_RECOMMENDATION,
        "detail": detail,
    }


def _average_score(values: Iterable[int]) -> int | None:
    scores = [int(value) for value in values]
    if not scores:
        return None
    return round(sum(scores) / len(scores))


def _agent_os_quality_scorecard(
    db: Session,
    *,
    repo_id: int,
    user_id: int | None,
) -> dict[str, Any]:
    since = _utcnow() - timedelta(days=AGENT_OS_QUALITY_WINDOW_DAYS)
    q = db.query(ProjectAutonomyRun).filter(
        ProjectAutonomyRun.repo_id == int(repo_id),
        ProjectAutonomyRun.created_at >= since,
    )
    if user_id is not None:
        q = q.filter(ProjectAutonomyRun.user_id == user_id)
    rows = (
        q.order_by(ProjectAutonomyRun.created_at.desc(), ProjectAutonomyRun.id.desc())
        .limit(AGENT_OS_QUALITY_RECENT_RUN_LIMIT)
        .all()
    )
    status_counts: dict[str, int] = {}
    review_scores: list[int] = []
    review_status_counts: dict[str, int] = {}
    review_stale_count = 0
    review_missing_for_approval = 0
    approval_gate_risk_count = 0
    validation_total = 0
    validation_passed_count = 0
    review_examples: list[dict[str, Any]] = []

    for row in rows:
        status = str(row.status or "")
        status_counts[status] = status_counts.get(status, 0) + 1
        plan = _json_load(row.plan_json, {})
        plan = plan if isinstance(plan, dict) else {}
        review = _latest_architect_review(db, row.run_id)
        review_payload = _architect_review_payload(review)
        if review_payload:
            review_payload["stale"] = not _architect_review_current_for_plan(
                review,
                prompt=row.prompt,
                plan=plan,
            )
            review_status = str(review_payload.get("status") or ARCHITECT_REVIEW_STATUS_FAILED)
            review_status_counts[review_status] = review_status_counts.get(review_status, 0) + 1
            review_scores.append(int(review_payload.get("score") or 0))
            if review_payload.get("stale") is True:
                review_stale_count += 1
            if len(review_examples) < AGENT_OS_QUALITY_RECENT_REVIEW_PREVIEW_LIMIT:
                review_examples.append(
                    {
                        "run_id": row.run_id,
                        "status": review_status,
                        "score": int(review_payload.get("score") or 0),
                        "stale": bool(review_payload.get("stale")),
                        "blocking_reason": review_payload.get("blocking_reason"),
                    }
                )
        elif status == RUN_STATUS_AWAITING_APPROVAL and plan:
            review_missing_for_approval += 1

        if status == RUN_STATUS_AWAITING_APPROVAL and not _architect_review_passed(review_payload):
            approval_gate_risk_count += 1

        validation = _json_load(row.validation_json, [])
        if isinstance(validation, list) and validation:
            validation_total += 1
            if validation_passed(validation):
                validation_passed_count += 1

    run_ids = [row.run_id for row in rows]
    quality_status_counts: dict[str, int] = {}
    quality_scores: list[int] = []
    quality_examples: list[dict[str, Any]] = []
    if run_ids:
        artifact_rows = (
            db.query(ProjectAutonomyArtifact)
            .filter(
                ProjectAutonomyArtifact.run_id.in_(run_ids),
                ProjectAutonomyArtifact.artifact_type == SCHEDULED_AGENT_REPORT_QUALITY_ARTIFACT_TYPE,
                ProjectAutonomyArtifact.created_at >= since,
            )
            .order_by(ProjectAutonomyArtifact.created_at.desc(), ProjectAutonomyArtifact.id.desc())
            .limit(AGENT_OS_QUALITY_RECENT_ARTIFACT_LIMIT)
            .all()
        )
        for artifact in artifact_rows:
            payload = _json_load(artifact.content_json, {})
            payload = payload if isinstance(payload, dict) else {}
            status = str(payload.get("status") or SCHEDULED_AGENT_REPORT_QUALITY_LOW)
            quality_status_counts[status] = quality_status_counts.get(status, 0) + 1
            if payload.get("score") is not None:
                quality_scores.append(int(payload.get("score") or 0))
            if len(quality_examples) < AGENT_OS_QUALITY_RECENT_REVIEW_PREVIEW_LIMIT:
                quality_examples.append(
                    {
                        "run_id": artifact.run_id,
                        "name": artifact.name,
                        "status": status,
                        "score": payload.get("score"),
                        "issues": payload.get("issues") or [],
                    }
                )

    terminal_count = sum(count for status, count in status_counts.items() if status in TERMINAL_STATUSES)
    blocked_failed_count = status_counts.get(RUN_STATUS_BLOCKED, 0) + status_counts.get(RUN_STATUS_FAILED, 0)
    blocked_ratio = (blocked_failed_count / terminal_count) if terminal_count else 0.0
    validation_failed_count = validation_total - validation_passed_count
    low_quality_count = quality_status_counts.get(SCHEDULED_AGENT_REPORT_QUALITY_LOW, 0)
    repaired_count = quality_status_counts.get(SCHEDULED_AGENT_REPORT_QUALITY_REPAIRED, 0)
    review_passed_count = review_status_counts.get(ARCHITECT_REVIEW_STATUS_PASSED, 0)
    review_failed_count = (
        review_status_counts.get(ARCHITECT_REVIEW_STATUS_FAILED, 0)
        + review_status_counts.get(ARCHITECT_REVIEW_STATUS_NEEDS_REVISION, 0)
        + review_status_counts.get(ARCHITECT_REVIEW_STATUS_NEEDS_CLARIFICATION, 0)
    )

    problems: list[str] = []
    if approval_gate_risk_count:
        problems.append(
            f"{approval_gate_risk_count} approval-ready run(s) lack a current passing architect review."
        )
    if low_quality_count:
        problems.append(f"{low_quality_count} scheduled-agent quality gate(s) rejected weak local-model output.")
    if (
        terminal_count >= AGENT_OS_QUALITY_TERMINAL_MIN_SAMPLE
        and blocked_ratio >= AGENT_OS_QUALITY_BLOCKED_WARNING_RATIO
    ):
        problems.append("Recent terminal Autopilot runs are mostly blocked or failed.")
    if validation_total >= AGENT_OS_QUALITY_TERMINAL_MIN_SAMPLE and validation_failed_count > validation_passed_count:
        problems.append("Recent validation results fail more often than they pass.")

    if not rows:
        detail = (
            "No recent Autopilot runs yet; architect review and scheduled-agent quality gates are installed."
        )
    elif problems:
        detail = " ".join(problems[:AGENT_OS_QUALITY_PROBLEM_PREVIEW_LIMIT])
    else:
        detail = (
            f"Recent guardrails are healthy: {review_passed_count} passing plan review(s), "
            f"{review_failed_count} blocked weak plan(s), {repaired_count} repaired schedule report(s), "
            f"and {validation_passed_count}/{validation_total} validation set(s) passed."
        )

    return {
        "status": AGENT_OS_READINESS_CHECK_WARNING if problems else AGENT_OS_READINESS_CHECK_PASSED,
        "detail": detail,
        "window_days": AGENT_OS_QUALITY_WINDOW_DAYS,
        "recent_run_count": len(rows),
        "status_counts": status_counts,
        "terminal_count": terminal_count,
        "blocked_failed_count": blocked_failed_count,
        "blocked_failed_ratio": round(blocked_ratio, 2),
        "architect_reviews": {
            "total": sum(review_status_counts.values()),
            "passed": review_passed_count,
            "blocked": review_failed_count,
            "stale": review_stale_count,
            "missing_for_approval": review_missing_for_approval,
            "average_score": _average_score(review_scores),
            "statuses": review_status_counts,
            "recent": review_examples,
        },
        "scheduled_quality": {
            "total": sum(quality_status_counts.values()),
            "passed": quality_status_counts.get(SCHEDULED_AGENT_REPORT_QUALITY_PASSED, 0),
            "repaired": repaired_count,
            "low_quality": low_quality_count,
            "average_score": _average_score(quality_scores),
            "statuses": quality_status_counts,
            "recent": quality_examples,
        },
        "validation": {
            "total": validation_total,
            "passed": validation_passed_count,
            "failed": validation_failed_count,
        },
        "approval_gate_risk_count": approval_gate_risk_count,
        "problems": problems[:AGENT_OS_QUALITY_PROBLEM_PREVIEW_LIMIT],
    }


def _agent_quality_monitor_dimension(
    key: str,
    label: str,
    status: str,
    detail: str,
    *,
    count: int | None = None,
    score: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "key": key,
        "label": label,
        "status": status,
        "detail": detail,
    }
    if count is not None:
        payload["count"] = int(count)
    if score is not None:
        payload["score"] = int(score)
    return payload


def _agent_quality_monitor_payload(
    *,
    quality_scorecard: Mapping[str, Any],
    runtime_queue: Mapping[str, Any],
    operator_inbox: Mapping[str, Any],
    local_model: Mapping[str, Any],
    codex_alignment: Mapping[str, Any],
) -> dict[str, Any]:
    reviews = _mapping_payload(quality_scorecard.get("architect_reviews"))
    scheduled = _mapping_payload(quality_scorecard.get("scheduled_quality"))
    validation = _mapping_payload(quality_scorecard.get("validation"))

    review_total = int(reviews.get("total") or 0)
    review_passed = int(reviews.get("passed") or 0)
    review_blocked = int(reviews.get("blocked") or 0)
    review_stale = int(reviews.get("stale") or 0)
    review_missing = int(reviews.get("missing_for_approval") or 0)
    approval_gate_risk = int(quality_scorecard.get("approval_gate_risk_count") or 0)
    if approval_gate_risk or review_missing:
        review_status = AGENT_OS_READINESS_CHECK_WARNING
        review_detail = (
            f"{approval_gate_risk or review_missing} approval-ready run(s) need a current passing architect review."
        )
    elif review_stale:
        review_status = AGENT_OS_READINESS_CHECK_WARNING
        review_detail = f"{review_stale} architect review(s) are stale and cannot prove current plan quality."
    elif review_total:
        review_status = AGENT_OS_READINESS_CHECK_PASSED
        review_detail = f"{review_passed} plan review(s) passed and {review_blocked} weak plan(s) were blocked."
    else:
        review_status = AGENT_OS_READINESS_CHECK_PASSED
        review_detail = "Architect review gate is installed; no recent plan reviews have run yet."

    scheduled_total = int(scheduled.get("total") or 0)
    scheduled_passed = int(scheduled.get("passed") or 0)
    scheduled_repaired = int(scheduled.get("repaired") or 0)
    scheduled_low = int(scheduled.get("low_quality") or 0)
    if scheduled_low:
        scheduled_status = AGENT_OS_READINESS_CHECK_WARNING
        scheduled_detail = f"{scheduled_low} scheduled-agent report(s) were rejected as low quality."
    elif scheduled_repaired:
        scheduled_status = AGENT_OS_READINESS_CHECK_WARNING
        scheduled_detail = f"{scheduled_repaired} scheduled-agent report(s) needed repair before being accepted."
    elif scheduled_total:
        scheduled_status = AGENT_OS_READINESS_CHECK_PASSED
        scheduled_detail = f"{scheduled_passed}/{scheduled_total} scheduled-agent quality checks passed."
    else:
        scheduled_status = AGENT_OS_READINESS_CHECK_PASSED
        scheduled_detail = "Scheduled-agent quality gate is ready; no recent cycle reports have been scored yet."

    validation_total = int(validation.get("total") or 0)
    validation_passed = int(validation.get("passed") or 0)
    validation_failed = int(validation.get("failed") or 0)
    if validation_total >= AGENT_OS_QUALITY_TERMINAL_MIN_SAMPLE and validation_failed > validation_passed:
        validation_status = AGENT_OS_READINESS_CHECK_WARNING
        validation_detail = f"Recent validations are weak: {validation_passed}/{validation_total} validation set(s) passed."
    elif validation_total:
        validation_status = AGENT_OS_READINESS_CHECK_PASSED
        validation_detail = f"{validation_passed}/{validation_total} recent validation set(s) passed."
    else:
        validation_status = AGENT_OS_READINESS_CHECK_PASSED
        validation_detail = "No validation history yet; implementation runs will populate this quality signal."

    model_status = str(local_model.get("status") or AGENT_OS_READINESS_CHECK_FAILED)
    model_detail = str(local_model.get("detail") or "Local model readiness is unknown.")
    codex_status = str(codex_alignment.get("status") or AGENT_OS_READINESS_CHECK_PASSED)
    codex_detail = str(codex_alignment.get("detail") or "No Codex automation comparison is required for this repo.")
    runtime_status = str(runtime_queue.get("status") or AGENT_OS_READINESS_CHECK_PASSED)
    runtime_detail = str(runtime_queue.get("detail") or "Runtime queue has not reported yet.")
    inbox_total = int(operator_inbox.get("total_action_count") or 0)
    inbox_status = (
        AGENT_OS_READINESS_CHECK_WARNING
        if inbox_total
        else AGENT_OS_READINESS_CHECK_PASSED
    )
    inbox_detail = (
        f"{inbox_total} operator action(s) are waiting."
        if inbox_total
        else "No operator questions, approvals, or blockers are waiting."
    )

    dimensions = [
        _agent_quality_monitor_dimension(
            AGENT_QUALITY_MONITOR_DIMENSION_ARCHITECT,
            "Architect reviews",
            review_status,
            review_detail,
            count=review_total,
            score=reviews.get("average_score"),
        ),
        _agent_quality_monitor_dimension(
            AGENT_QUALITY_MONITOR_DIMENSION_SCHEDULED,
            "Scheduled-agent output",
            scheduled_status,
            scheduled_detail,
            count=scheduled_total,
            score=scheduled.get("average_score"),
        ),
        _agent_quality_monitor_dimension(
            AGENT_QUALITY_MONITOR_DIMENSION_VALIDATION,
            "Validation evidence",
            validation_status,
            validation_detail,
            count=validation_total,
        ),
        _agent_quality_monitor_dimension(
            AGENT_QUALITY_MONITOR_DIMENSION_MODEL,
            "Local model",
            model_status,
            model_detail,
            count=len(local_model.get("installed_models") or []),
        ),
        _agent_quality_monitor_dimension(
            AGENT_QUALITY_MONITOR_DIMENSION_CODEX,
            "Codex comparison",
            codex_status,
            codex_detail,
            count=int(codex_alignment.get("reference_count") or 0),
            score=codex_alignment.get("score"),
        ),
        _agent_quality_monitor_dimension(
            AGENT_QUALITY_MONITOR_DIMENSION_RUNTIME,
            "Runtime flow",
            runtime_status,
            runtime_detail,
            count=int(runtime_queue.get("open_count") or 0),
        ),
        _agent_quality_monitor_dimension(
            AGENT_QUALITY_MONITOR_DIMENSION_INBOX,
            "Operator inbox",
            inbox_status,
            inbox_detail,
            count=inbox_total,
        ),
    ]
    failed_count = sum(1 for dimension in dimensions if dimension["status"] == AGENT_OS_READINESS_CHECK_FAILED)
    warning_count = sum(1 for dimension in dimensions if dimension["status"] == AGENT_OS_READINESS_CHECK_WARNING)
    score_points = 0.0
    for dimension in dimensions:
        dimension_status = str(dimension.get("status") or "")
        if dimension_status == AGENT_OS_READINESS_CHECK_PASSED:
            score_points += AGENT_CODEX_ALIGNMENT_PASS_POINTS
        elif dimension_status == AGENT_OS_READINESS_CHECK_WARNING:
            score_points += AGENT_CODEX_ALIGNMENT_WARNING_POINTS
        else:
            score_points += AGENT_CODEX_ALIGNMENT_FAIL_POINTS
    score = round((score_points / max(len(dimensions), 1)) * 100)
    status = (
        AGENT_OS_READINESS_CHECK_FAILED
        if failed_count
        else (
            AGENT_OS_READINESS_CHECK_WARNING
            if warning_count
            else AGENT_OS_READINESS_CHECK_PASSED
        )
    )
    problems = [
        str(dimension.get("detail") or "")
        for dimension in dimensions
        if dimension.get("status") != AGENT_OS_READINESS_CHECK_PASSED
    ]

    if model_status == AGENT_OS_READINESS_CHECK_FAILED or not bool(local_model.get("coding_ready")):
        next_action = AGENT_QUALITY_MONITOR_ACTION_INSTALL_MODEL
        next_action_label = "Install coder model"
        next_action_detail = str(local_model.get("recommendation") or AGENT_OS_LOCAL_MODEL_RECOMMENDATION)
    elif approval_gate_risk or review_missing or review_stale:
        next_action = AGENT_QUALITY_MONITOR_ACTION_REVIEW_PLANS
        next_action_label = "Review plan gate"
        next_action_detail = review_detail
    elif scheduled_low or scheduled_repaired:
        next_action = AGENT_QUALITY_MONITOR_ACTION_REVIEW_REPORTS
        next_action_label = "Review scheduled reports"
        next_action_detail = scheduled_detail
    elif validation_status != AGENT_OS_READINESS_CHECK_PASSED:
        next_action = AGENT_QUALITY_MONITOR_ACTION_RUN_VALIDATION
        next_action_label = "Run validation"
        next_action_detail = validation_detail
    elif codex_status != AGENT_OS_READINESS_CHECK_PASSED:
        next_action = AGENT_QUALITY_MONITOR_ACTION_SYNC_CODEX
        next_action_label = "Sync Codex profiles"
        next_action_detail = codex_detail
    elif runtime_status != AGENT_OS_READINESS_CHECK_PASSED:
        next_action = AGENT_QUALITY_MONITOR_ACTION_DRAIN_QUEUE
        next_action_label = "Drain runtime queue"
        next_action_detail = runtime_detail
    elif inbox_total:
        next_action = AGENT_QUALITY_MONITOR_ACTION_ANSWER_INBOX
        next_action_label = "Answer operator inbox"
        next_action_detail = inbox_detail
    else:
        next_action = AGENT_QUALITY_MONITOR_ACTION_KEEP_MONITORING
        next_action_label = "Keep monitoring"
        next_action_detail = "Local quality signals are inside the current guardrails."

    detail = (
        "Local quality monitor is blocked: " + problems[0]
        if failed_count and problems
        else (
            "Local quality monitor needs attention: "
            + " ".join(problems[:AGENT_OS_QUALITY_PROBLEM_PREVIEW_LIMIT])
            if problems
            else "Local quality monitor is healthy across architect review, scheduled output, validation, model, runtime, and operator signals."
        )
    )
    return {
        "status": status,
        "score": score,
        "detail": detail,
        "dimension_count": len(dimensions),
        "warning_count": warning_count,
        "failed_count": failed_count,
        "dimensions": dimensions,
        "problems": problems[:AGENT_OS_QUALITY_PROBLEM_PREVIEW_LIMIT],
        "next_action": next_action,
        "next_action_label": next_action_label,
        "next_action_detail": next_action_detail,
    }


def _bounded_quality_score(raw: Any, *, default: int = 0) -> int:
    try:
        score = int(raw)
    except (TypeError, ValueError):
        score = default
    return max(0, min(100, score))


def _quality_status_score(status: str, *, passed: int = 100, warning: int = 70, failed: int = 25) -> int:
    if status == AGENT_OS_READINESS_CHECK_PASSED:
        return passed
    if status == AGENT_OS_READINESS_CHECK_WARNING:
        return warning
    return failed


def _agent_coding_quality_bar_dimension(
    key: str,
    label: str,
    score: int,
    detail: str,
    *,
    next_action: str,
    next_action_label: str,
    next_action_detail: str = "",
) -> dict[str, Any]:
    bounded = _bounded_quality_score(score)
    return {
        "key": key,
        "label": label,
        "score": bounded,
        "status": (
            AGENT_OS_READINESS_CHECK_PASSED
            if bounded >= AGENT_CODING_QUALITY_BAR_TARGET_SCORE
            else AGENT_OS_READINESS_CHECK_WARNING
        ),
        "detail": detail,
        "next_action": next_action,
        "next_action_label": next_action_label,
        "next_action_detail": next_action_detail,
    }


def _agent_coding_quality_bar_payload(
    *,
    local_model: Mapping[str, Any],
    quality_monitor: Mapping[str, Any],
    capability_audit: Mapping[str, Any],
    codex_alignment: Mapping[str, Any],
    runtime_queue: Mapping[str, Any],
    operator_inbox: Mapping[str, Any],
) -> dict[str, Any]:
    local_model_ready = bool(local_model.get("coding_ready"))
    local_model_score = 100 if local_model_ready else 60 if bool(local_model.get("available")) else 25
    runtime_status = str(runtime_queue.get("status") or AGENT_OS_READINESS_CHECK_WARNING)
    inbox_total = int(operator_inbox.get("total_action_count") or 0)
    inbox_next_action = str(operator_inbox.get("next_action") or "").strip()
    inbox_has_next_action = bool(operator_inbox.get("next_action_label"))
    inbox_score = (
        100
        if inbox_total == 0
        else 82
        if inbox_has_next_action and inbox_next_action != AGENT_OPERATOR_INBOX_ACTION_KEEP_MONITORING
        else 45
    )
    dimensions = [
        _agent_coding_quality_bar_dimension(
            AGENT_CODING_QUALITY_BAR_DIMENSION_LOCAL_MODEL,
            "Local model bridge",
            local_model_score,
            str(local_model.get("detail") or "Local model readiness has not reported yet."),
            next_action=(
                AGENT_CODING_QUALITY_BAR_ACTION_KEEP_MONITORING
                if local_model_ready
                else AGENT_CODING_QUALITY_BAR_ACTION_INSTALL_MODEL
            ),
            next_action_label="Keep monitoring" if local_model_ready else "Install coder model",
            next_action_detail=str(local_model.get("recommendation") or AGENT_OS_LOCAL_MODEL_RECOMMENDATION),
        ),
        _agent_coding_quality_bar_dimension(
            AGENT_CODING_QUALITY_BAR_DIMENSION_QUALITY,
            "Quality governance",
            _bounded_quality_score(quality_monitor.get("score")),
            str(quality_monitor.get("detail") or "Quality monitor has not reported yet."),
            next_action=str(
                quality_monitor.get("next_action")
                or AGENT_CODING_QUALITY_BAR_ACTION_REVIEW_QUALITY
            ),
            next_action_label=str(quality_monitor.get("next_action_label") or "Review quality"),
            next_action_detail=str(quality_monitor.get("next_action_detail") or ""),
        ),
        _agent_coding_quality_bar_dimension(
            AGENT_CODING_QUALITY_BAR_DIMENSION_CAPABILITY,
            "Agent OS capability",
            _bounded_quality_score(capability_audit.get("score")),
            str(capability_audit.get("detail") or "Agent OS capability audit has not reported yet."),
            next_action=str(
                capability_audit.get("next_action")
                or AGENT_CODING_QUALITY_BAR_ACTION_REVIEW_CAPABILITY
            ),
            next_action_label=str(capability_audit.get("next_action_label") or "Review capability"),
            next_action_detail=str(capability_audit.get("next_action_detail") or ""),
        ),
        _agent_coding_quality_bar_dimension(
            AGENT_CODING_QUALITY_BAR_DIMENSION_CODEX,
            "Codex/Claude parity",
            _bounded_quality_score(codex_alignment.get("score"), default=100),
            str(codex_alignment.get("detail") or "No Codex automation comparison is required for this repo."),
            next_action=(
                AGENT_CODING_QUALITY_BAR_ACTION_KEEP_MONITORING
                if str(codex_alignment.get("status") or "") == AGENT_OS_READINESS_CHECK_PASSED
                else AGENT_CODING_QUALITY_BAR_ACTION_REVIEW_CODEX
            ),
            next_action_label=(
                "Keep monitoring"
                if str(codex_alignment.get("status") or "") == AGENT_OS_READINESS_CHECK_PASSED
                else "Review Codex parity"
            ),
            next_action_detail=str(codex_alignment.get("detail") or ""),
        ),
        _agent_coding_quality_bar_dimension(
            AGENT_CODING_QUALITY_BAR_DIMENSION_RUNTIME,
            "Runtime control",
            _quality_status_score(runtime_status),
            str(runtime_queue.get("detail") or "Runtime queue has not reported yet."),
            next_action=(
                AGENT_CODING_QUALITY_BAR_ACTION_KEEP_MONITORING
                if runtime_status == AGENT_OS_READINESS_CHECK_PASSED
                else AGENT_CODING_QUALITY_BAR_ACTION_DRAIN_QUEUE
            ),
            next_action_label="Keep monitoring" if runtime_status == AGENT_OS_READINESS_CHECK_PASSED else "Drain queue",
            next_action_detail=str(runtime_queue.get("detail") or ""),
        ),
        _agent_coding_quality_bar_dimension(
            AGENT_CODING_QUALITY_BAR_DIMENSION_OPERATOR,
            "Operator recovery",
            inbox_score,
            str(operator_inbox.get("detail") or "Operator inbox has not reported yet."),
            next_action=(
                AGENT_CODING_QUALITY_BAR_ACTION_KEEP_MONITORING
                if inbox_total == 0
                else AGENT_CODING_QUALITY_BAR_ACTION_ANSWER_INBOX
            ),
            next_action_label="Keep monitoring" if inbox_total == 0 else "Answer operator inbox",
            next_action_detail=str(operator_inbox.get("next_action_detail") or operator_inbox.get("detail") or ""),
        ),
    ]
    score = round(sum(int(dimension["score"]) for dimension in dimensions) / max(len(dimensions), 1))
    gaps = [
        dimension
        for dimension in dimensions
        if int(dimension.get("score") or 0) < AGENT_CODING_QUALITY_BAR_TARGET_SCORE
    ]
    competitive = score >= AGENT_CODING_QUALITY_BAR_TARGET_SCORE and not gaps
    if gaps:
        weakest = min(gaps, key=lambda item: int(item.get("score") or 0))
        next_action = str(weakest.get("next_action") or AGENT_CODING_QUALITY_BAR_ACTION_REVIEW_QUALITY)
        next_action_label = str(weakest.get("next_action_label") or "Review quality bar")
        next_action_detail = str(weakest.get("detail") or "")
        detail = (
            f"Local coding cockpit is {score}/100 against the "
            f"{AGENT_CODING_QUALITY_BAR_TARGET_SCORE}/100 Codex/Claude-class target; "
            f"weakest signal is {weakest.get('label')} at {weakest.get('score')}/100."
        )
    else:
        next_action = AGENT_CODING_QUALITY_BAR_ACTION_KEEP_MONITORING
        next_action_label = "Keep monitoring"
        next_action_detail = "The local coding cockpit is at or above the current Codex/Claude-class operating bar."
        detail = (
            f"Local coding cockpit is {score}/100 against the "
            f"{AGENT_CODING_QUALITY_BAR_TARGET_SCORE}/100 Codex/Claude-class target."
        )
    return {
        "status": (
            AGENT_OS_READINESS_CHECK_PASSED
            if competitive
            else AGENT_OS_READINESS_CHECK_WARNING
        ),
        "score": score,
        "target_score": AGENT_CODING_QUALITY_BAR_TARGET_SCORE,
        "competitive": competitive,
        "detail": detail,
        "dimensions": dimensions,
        "gaps": gaps[:AGENT_OS_QUALITY_PROBLEM_PREVIEW_LIMIT],
        "next_action": next_action,
        "next_action_label": next_action_label,
        "next_action_detail": next_action_detail,
    }


def _agent_runtime_queue_state(
    db: Session,
    *,
    repo_id: int,
    user_id: int | None,
    profiles: list[dict[str, Any]],
) -> dict[str, Any]:
    profile_ids = {
        int(profile["id"])
        for profile in profiles
        if profile.get("id") is not None
    }
    always_on_profile_ids = {
        int(profile["id"])
        for profile in profiles
        if profile.get("id") is not None
        and profile.get("schedule_enabled")
        and str((_mapping_payload(profile.get("schedule"))).get(AGENT_RUNTIME_MODE_KEY) or "")
        == AGENT_RUNTIME_MODE_ALWAYS_ON
    }
    q = db.query(ProjectAutonomyRun).filter(
        ProjectAutonomyRun.repo_id == int(repo_id),
        ProjectAutonomyRun.archived_at.is_(None),
    )
    if user_id is not None:
        q = q.filter(ProjectAutonomyRun.user_id == user_id)
    rows = (
        q.order_by(ProjectAutonomyRun.created_at.desc(), ProjectAutonomyRun.id.desc())
        .limit(AGENT_RUNTIME_QUEUE_RECENT_LIMIT)
        .all()
    )
    status_counts: dict[str, int] = {}
    queued_runs: list[ProjectAutonomyRun] = []
    active_runs: list[ProjectAutonomyRun] = []
    waiting_runs: list[ProjectAutonomyRun] = []
    always_on_open_runs: list[ProjectAutonomyRun] = []
    queue_active_statuses = ACTIVE_STATUSES - {RUN_STATUS_QUEUED}
    now = _utcnow()
    stale_cutoff = now - timedelta(minutes=AGENT_RUNTIME_QUEUE_STALE_ACTIVE_MINUTES)
    for row in rows:
        status = str(row.status or "")
        status_counts[status] = status_counts.get(status, 0) + 1
        if status == RUN_STATUS_QUEUED:
            queued_runs.append(row)
        if status in queue_active_statuses:
            active_runs.append(row)
        if status in IDLE_STATUSES:
            waiting_runs.append(row)
        if (
            row.agent_profile_id is not None
            and int(row.agent_profile_id) in always_on_profile_ids
            and status not in TERMINAL_STATUSES
        ):
            always_on_open_runs.append(row)
    stale_active_runs = [
        row
        for row in active_runs
        if (row.updated_at or row.started_at or row.created_at)
        and (row.updated_at or row.started_at or row.created_at) < stale_cutoff
    ]
    stale_active_run_ids = {row.run_id for row in stale_active_runs}
    fresh_active_runs = [
        row for row in active_runs if row.run_id not in stale_active_run_ids
    ]

    pending_questions = (
        db.query(ProjectAutonomyOperatorQuestion)
        .filter(
            ProjectAutonomyOperatorQuestion.repo_id == int(repo_id),
            ProjectAutonomyOperatorQuestion.status == OPERATOR_QUESTION_STATUS_PENDING,
        )
    )
    if user_id is not None:
        pending_questions = pending_questions.filter(ProjectAutonomyOperatorQuestion.user_id == user_id)
    pending_question_count = pending_questions.count()

    queued_count = len(queued_runs)
    active_count = len(active_runs)
    stale_active_count = len(stale_active_runs)
    fresh_active_count = max(0, active_count - stale_active_count)
    waiting_count = len(waiting_runs)
    open_count = sum(1 for row in rows if str(row.status or "") not in TERMINAL_STATUSES)
    problems: list[str] = []
    if queued_count > AGENT_RUNTIME_QUEUE_WARNING_DEPTH:
        problems.append(f"{queued_count} queued run(s) are waiting for workers.")
    if stale_active_runs:
        problems.append(
            f"{len(stale_active_runs)} active run(s) have not reported progress for "
            f"{AGENT_RUNTIME_QUEUE_STALE_ACTIVE_MINUTES}+ minute(s)."
        )
    if always_on_profile_ids and len(always_on_open_runs) > len(always_on_profile_ids):
        problems.append("Always-on agents have more open runs than active runtime profiles.")
    if pending_question_count:
        problems.append(f"{pending_question_count} operator question(s) are blocking progress.")

    if problems:
        detail = " ".join(problems[:AGENT_OS_QUALITY_PROBLEM_PREVIEW_LIMIT])
    elif always_on_profile_ids:
        detail = (
            f"Always-on queue is controlled: {queued_count} queued, {active_count} active, "
            f"{waiting_count} waiting for approval or discussion."
        )
    else:
        detail = (
            f"Runtime queue is quiet: {queued_count} queued, {active_count} active, "
            f"{waiting_count} waiting."
        )

    def _run_preview(row: ProjectAutonomyRun) -> dict[str, Any]:
        last_seen_at = row.updated_at or row.started_at or row.created_at
        last_seen_age_minutes = (
            max(0, int((now - last_seen_at).total_seconds() // 60))
            if last_seen_at
            else None
        )
        return {
            "run_id": row.run_id,
            "agent_profile_id": row.agent_profile_id,
            "status": row.status,
            "stage": row.current_stage,
            "plan_status": row.plan_status,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "last_seen_at": last_seen_at.isoformat() if last_seen_at else None,
            "last_seen_age_minutes": last_seen_age_minutes,
        }

    if stale_active_runs:
        next_action = AGENT_RUNTIME_QUEUE_ACTION_INSPECT_STALE
        next_action_label = "Inspect stale run"
        next_action_detail = (
            "Open the stale active run and recover or restart it if the worker is no longer progressing."
        )
        next_action_run = _run_preview(stale_active_runs[0])
    elif active_runs:
        next_action = AGENT_RUNTIME_QUEUE_ACTION_INSPECT_ACTIVE
        next_action_label = "Inspect active run"
        next_action_detail = "Open the active run and confirm the worker is still making progress."
        next_action_run = _run_preview(active_runs[0])
    elif queued_runs:
        next_action = AGENT_RUNTIME_QUEUE_ACTION_DRAIN_QUEUED
        next_action_label = "Drain queued run"
        next_action_detail = "Open the queued run, then start or inspect the runtime worker if it is not moving."
        next_action_run = _run_preview(queued_runs[0])
    elif waiting_runs:
        next_action = AGENT_RUNTIME_QUEUE_ACTION_REVIEW_WAITING
        next_action_label = "Review waiting run"
        next_action_detail = "Open the waiting run and handle the approval, clarification, or chat handoff."
        next_action_run = _run_preview(waiting_runs[0])
    else:
        next_action = AGENT_RUNTIME_QUEUE_ACTION_KEEP_MONITORING
        next_action_label = "Keep monitoring"
        next_action_detail = "No open runtime work needs operator recovery."
        next_action_run = {}

    return {
        "status": AGENT_OS_READINESS_CHECK_WARNING if problems else AGENT_OS_READINESS_CHECK_PASSED,
        "detail": detail,
        "recent_limit": AGENT_RUNTIME_QUEUE_RECENT_LIMIT,
        "profile_count": len(profile_ids),
        "always_on_profile_count": len(always_on_profile_ids),
        "open_count": open_count,
        "queued_count": queued_count,
        "active_count": active_count,
        "fresh_active_count": fresh_active_count,
        "waiting_count": waiting_count,
        "stale_active_count": stale_active_count,
        "stale_after_minutes": AGENT_RUNTIME_QUEUE_STALE_ACTIVE_MINUTES,
        "pending_question_count": pending_question_count,
        "always_on_open_count": len(always_on_open_runs),
        "status_counts": status_counts,
        "problems": problems[:AGENT_OS_QUALITY_PROBLEM_PREVIEW_LIMIT],
        "next_action": next_action,
        "next_action_label": next_action_label,
        "next_action_detail": next_action_detail,
        "next_action_run_id": next_action_run.get("run_id"),
        "next_action_agent_profile_id": next_action_run.get("agent_profile_id"),
        "next_action_status": next_action_run.get("status"),
        "next_action_stage": next_action_run.get("stage"),
        "next_action_plan_status": next_action_run.get("plan_status"),
        "next_action_last_seen_at": next_action_run.get("last_seen_at"),
        "next_action_last_seen_age_minutes": next_action_run.get("last_seen_age_minutes"),
        "stale_active_runs": [
            _run_preview(row) for row in stale_active_runs[:AGENT_RUNTIME_QUEUE_PREVIEW_LIMIT]
        ],
        "fresh_active_runs": [
            _run_preview(row) for row in fresh_active_runs[:AGENT_RUNTIME_QUEUE_PREVIEW_LIMIT]
        ],
        "queued_runs": [_run_preview(row) for row in queued_runs[:AGENT_RUNTIME_QUEUE_PREVIEW_LIMIT]],
        "active_runs": [_run_preview(row) for row in active_runs[:AGENT_RUNTIME_QUEUE_PREVIEW_LIMIT]],
        "waiting_runs": [_run_preview(row) for row in waiting_runs[:AGENT_RUNTIME_QUEUE_PREVIEW_LIMIT]],
    }


def _agent_operator_inbox_state(
    db: Session,
    *,
    repo_id: int,
    user_id: int | None,
    profiles: list[dict[str, Any]],
) -> dict[str, Any]:
    profile_names = {
        int(profile["id"]): str(profile.get("name") or profile.get("profile_key") or "Agent")
        for profile in profiles
        if profile.get("id") is not None
    }
    q = db.query(ProjectAutonomyRun).filter(
        ProjectAutonomyRun.repo_id == int(repo_id),
        ProjectAutonomyRun.archived_at.is_(None),
    )
    if user_id is not None:
        q = q.filter(ProjectAutonomyRun.user_id == user_id)
    rows = (
        q.order_by(ProjectAutonomyRun.created_at.desc(), ProjectAutonomyRun.id.desc())
        .limit(AGENT_OPERATOR_INBOX_RECENT_LIMIT)
        .all()
    )

    question_q = db.query(ProjectAutonomyOperatorQuestion).filter(
        ProjectAutonomyOperatorQuestion.repo_id == int(repo_id),
        ProjectAutonomyOperatorQuestion.status == OPERATOR_QUESTION_STATUS_PENDING,
    )
    if user_id is not None:
        question_q = question_q.filter(ProjectAutonomyOperatorQuestion.user_id == user_id)
    pending_questions = (
        question_q.order_by(
            ProjectAutonomyOperatorQuestion.created_at.desc(),
            ProjectAutonomyOperatorQuestion.id.desc(),
        )
        .limit(AGENT_OPERATOR_INBOX_PREVIEW_LIMIT)
        .all()
    )
    pending_question_count = question_q.count()

    def _agent_name(agent_profile_id: int | None) -> str:
        if agent_profile_id is None:
            return "Autopilot"
        return profile_names.get(int(agent_profile_id), "Autopilot agent")

    def _run_item(row: ProjectAutonomyRun, *, kind: str, label: str, reason: str) -> dict[str, Any]:
        return {
            "kind": kind,
            "label": label,
            "run_id": row.run_id,
            "agent_profile_id": row.agent_profile_id,
            "agent": _agent_name(row.agent_profile_id),
            "status": row.status,
            "plan_status": row.plan_status,
            "reason": truncate_text(reason, 240)[0],
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    approval_runs: list[ProjectAutonomyRun] = []
    clarification_runs: list[ProjectAutonomyRun] = []
    blocked_runs: list[ProjectAutonomyRun] = []
    user_reply_runs: list[ProjectAutonomyRun] = []
    items: list[dict[str, Any]] = []

    for question in pending_questions:
        items.append(
            {
                "kind": AGENT_OPERATOR_INBOX_ITEM_QUESTION,
                "label": "Operator question",
                "run_id": question.run_id,
                "agent_profile_id": question.agent_profile_id,
                "agent": _agent_name(question.agent_profile_id),
                "status": question.status,
                "reason": truncate_text(question.question, 240)[0],
                "created_at": question.created_at.isoformat() if question.created_at else None,
            }
        )

    for row in rows:
        status = str(row.status or "")
        plan_status = str(row.plan_status or "")
        if status == RUN_STATUS_AWAITING_APPROVAL or plan_status == PLAN_STATUS_AWAITING_APPROVAL:
            approval_runs.append(row)
            items.append(
                _run_item(
                    row,
                    kind=AGENT_OPERATOR_INBOX_ITEM_APPROVAL,
                    label="Approval needed",
                    reason="Review the plan and approve only if the architect quality gate passed.",
                )
            )
        elif status == RUN_STATUS_AWAITING_CLARIFICATION or plan_status == PLAN_STATUS_AWAITING_CLARIFICATION:
            clarification_runs.append(row)
            items.append(
                _run_item(
                    row,
                    kind=AGENT_OPERATOR_INBOX_ITEM_CLARIFICATION,
                    label="Clarification needed",
                    reason=row.error_message
                    or "CHILI needs a concrete operator answer before this agent can draft an approval-ready plan.",
                )
            )
        elif status in {RUN_STATUS_BLOCKED, RUN_STATUS_FAILED}:
            blocked_runs.append(row)
            items.append(
                _run_item(
                    row,
                    kind=AGENT_OPERATOR_INBOX_ITEM_BLOCKER,
                    label="Blocked handoff",
                    reason=row.error_message
                    or row.merge_message
                    or "This agent stopped before completion and needs operator review or a rerun.",
                )
            )
        if status == RUN_STATUS_CHATTING:
            latest_message = (
                db.query(ProjectAutonomyMessage)
                .filter(ProjectAutonomyMessage.run_id == row.run_id)
                .order_by(ProjectAutonomyMessage.id.desc())
                .first()
            )
            if latest_message is not None and latest_message.role == AGENT_OPERATOR_INBOX_USER_ROLE:
                user_reply_runs.append(row)
                items.append(
                    _run_item(
                        row,
                        kind=AGENT_OPERATOR_INBOX_ITEM_USER_REPLY,
                        label="Reply waiting",
                        reason="The operator sent the latest chat message and CHILI has not answered yet.",
                    )
                )

    total_action_count = (
        pending_question_count
        + len(approval_runs)
        + len(clarification_runs)
        + len(blocked_runs)
        + len(user_reply_runs)
    )
    action_by_kind = {
        AGENT_OPERATOR_INBOX_ITEM_QUESTION: (
            AGENT_OPERATOR_INBOX_ACTION_ANSWER_QUESTION,
            "Answer question",
        ),
        AGENT_OPERATOR_INBOX_ITEM_CLARIFICATION: (
            AGENT_OPERATOR_INBOX_ACTION_ANSWER_CLARIFICATION,
            "Answer clarification",
        ),
        AGENT_OPERATOR_INBOX_ITEM_APPROVAL: (
            AGENT_OPERATOR_INBOX_ACTION_REVIEW_APPROVAL,
            "Review approval",
        ),
        AGENT_OPERATOR_INBOX_ITEM_USER_REPLY: (
            AGENT_OPERATOR_INBOX_ACTION_CONTINUE_REPLY,
            "Continue reply",
        ),
        AGENT_OPERATOR_INBOX_ITEM_BLOCKER: (
            AGENT_OPERATOR_INBOX_ACTION_REVIEW_BLOCKER,
            "Review blocker",
        ),
    }
    priority_order = (
        AGENT_OPERATOR_INBOX_ITEM_QUESTION,
        AGENT_OPERATOR_INBOX_ITEM_CLARIFICATION,
        AGENT_OPERATOR_INBOX_ITEM_APPROVAL,
        AGENT_OPERATOR_INBOX_ITEM_USER_REPLY,
        AGENT_OPERATOR_INBOX_ITEM_BLOCKER,
    )
    next_action_item = next(
        (
            item
            for kind in priority_order
            for item in items
            if str(item.get("kind") or "") == kind
        ),
        None,
    )
    if next_action_item is not None:
        next_action_kind = str(next_action_item.get("kind") or "")
        next_action, next_action_label = action_by_kind.get(
            next_action_kind,
            (AGENT_OPERATOR_INBOX_ACTION_KEEP_MONITORING, "Keep monitoring"),
        )
        next_action_detail = str(next_action_item.get("reason") or "").strip()
        next_action_run_id = next_action_item.get("run_id")
        next_action_agent = str(next_action_item.get("agent") or "").strip()
    else:
        next_action = AGENT_OPERATOR_INBOX_ACTION_KEEP_MONITORING
        next_action_label = "Keep monitoring"
        next_action_kind = ""
        next_action_detail = "No operator approvals, answers, replies, or blocked handoffs are waiting."
        next_action_run_id = None
        next_action_agent = ""
    if total_action_count:
        parts = [
            (len(approval_runs), "approval"),
            (len(clarification_runs), "clarification"),
            (pending_question_count, "question"),
            (len(user_reply_runs), "reply"),
            (len(blocked_runs), "blocked handoff"),
        ]
        detail_bits = [f"{count} {label}(s)" for count, label in parts if count]
        detail = "Operator inbox has " + ", ".join(detail_bits) + "."
    else:
        detail = "Operator inbox is clear: no approvals, clarifications, questions, replies, or blocked handoffs need attention."

    return {
        "status": (
            AGENT_OS_READINESS_CHECK_WARNING
            if total_action_count
            else AGENT_OS_READINESS_CHECK_PASSED
        ),
        "detail": detail,
        "recent_limit": AGENT_OPERATOR_INBOX_RECENT_LIMIT,
        "total_action_count": total_action_count,
        "approval_count": len(approval_runs),
        "clarification_count": len(clarification_runs),
        "pending_question_count": pending_question_count,
        "reply_waiting_count": len(user_reply_runs),
        "blocked_count": len(blocked_runs),
        "next_action": next_action,
        "next_action_label": next_action_label,
        "next_action_detail": truncate_text(next_action_detail, 240)[0],
        "next_action_kind": next_action_kind,
        "next_action_run_id": next_action_run_id,
        "next_action_agent": next_action_agent,
        "items": items[:AGENT_OPERATOR_INBOX_PREVIEW_LIMIT],
    }


def agent_os_readiness(
    db: Session,
    *,
    repo_id: int,
    user_id: int | None = None,
) -> dict[str, Any]:
    repo = _resolve_repo_for_run(db, repo_id, user_id=user_id)
    if repo is None:
        raise ValueError("Repo not found.")
    repo = _canonical_repo_for_autopilot(db, repo, user_id=user_id)
    profiles = list_agent_profiles(db, repo_id=int(repo.id), user_id=user_id)
    codex_definitions = _codex_automation_definitions_for_repo(repo)
    codex_keys = {str(definition["profile_key"]) for definition in codex_definitions}
    profile_keys = {str(profile.get("profile_key") or "") for profile in profiles}
    imported_codex = [
        profile
        for profile in profiles
        if str(profile.get("profile_key") or "") in codex_keys
        or (profile.get("prompt_setting") or {}).get("source") == CODEX_AUTOMATION_SOURCE
    ]
    current_imported_codex = [
        profile
        for profile in imported_codex
        if str(profile.get("profile_key") or "") in codex_keys
    ]
    historical_codex = [
        profile
        for profile in imported_codex
        if str(profile.get("profile_key") or "") not in codex_keys
    ]
    stale_codex = [
        profile
        for profile in current_imported_codex
        if (_mapping_payload(profile.get("prompt_freshness")).get("status"))
        in {CODEX_AUTOMATION_SYNC_STATUS_STALE, CODEX_AUTOMATION_SYNC_STATUS_MISSING_SOURCE}
    ]
    custom_codex = [
        profile
        for profile in imported_codex
        if _mapping_payload(profile.get("prompt_freshness")).get("status") == CODEX_AUTOMATION_SYNC_STATUS_CUSTOM
    ]
    codex_schedule_counts = _codex_schedule_counts(imported_codex)
    codex_profile_matrix = _codex_automation_profile_matrix(codex_definitions, profiles)
    codex_contract_coverage = _codex_operating_contract_coverage(imported_codex)
    agent_teams = _agent_team_topology(profiles)
    worktree_enabled = [
        profile
        for profile in profiles
        if bool((profile.get("permissions") or {}).get(AGENT_PERMISSION_WORKTREE))
    ]
    merge_enabled = [
        profile
        for profile in profiles
        if bool((profile.get("permissions") or {}).get(AGENT_PERMISSION_MERGE))
    ]
    active_schedules = [
        profile
        for profile in profiles
        if profile.get("schedule_enabled")
        and str((profile.get("schedule") or {}).get("status") or "") == AGENT_SCHEDULE_STATUS_ACTIVE
    ]
    always_on_schedules = [
        profile
        for profile in active_schedules
        if str((profile.get("schedule") or {}).get(AGENT_RUNTIME_MODE_KEY) or "")
        == AGENT_RUNTIME_MODE_ALWAYS_ON
    ]
    pending_questions = sum(int(profile.get("pending_question_count") or 0) for profile in profiles)
    runtime_path = resolve_repo_runtime_path(repo)
    repo_ready = runtime_path is not None and runtime_path.exists()
    local_model = _local_model_readiness_payload(select_local_model())
    quality_scorecard = _agent_os_quality_scorecard(db, repo_id=int(repo.id), user_id=user_id)
    runtime_queue = _agent_runtime_queue_state(
        db,
        repo_id=int(repo.id),
        user_id=user_id,
        profiles=profiles,
    )
    operator_inbox = _agent_operator_inbox_state(
        db,
        repo_id=int(repo.id),
        user_id=user_id,
        profiles=profiles,
    )
    missing_profile_keys = sorted(codex_keys - profile_keys)
    codex_alignment = _agent_codex_alignment_scorecard(
        matching_count=len(codex_definitions),
        imported_codex=imported_codex,
        missing_profile_keys=missing_profile_keys,
        stale_codex=stale_codex,
        codex_contract_coverage=codex_contract_coverage,
        quality_scorecard=quality_scorecard,
        runtime_queue=runtime_queue,
        operator_inbox=operator_inbox,
        local_model=local_model,
    )
    quality_monitor = _agent_quality_monitor_payload(
        quality_scorecard=quality_scorecard,
        runtime_queue=runtime_queue,
        operator_inbox=operator_inbox,
        local_model=local_model,
        codex_alignment=codex_alignment,
    )
    codex_bench = _agent_codex_bench_payload(
        matching_count=len(codex_definitions),
        imported_codex=imported_codex,
        current_imported_codex=current_imported_codex,
        historical_codex=historical_codex,
        stale_codex=stale_codex,
        custom_codex=custom_codex,
        missing_profile_keys=missing_profile_keys,
        codex_schedule_counts=codex_schedule_counts,
        codex_contract_coverage=codex_contract_coverage,
        codex_alignment=codex_alignment,
    )
    capability_audit = _agent_os_capability_audit(
        repo_ready=repo_ready,
        runtime_path=runtime_path,
        profiles=profiles,
        agent_teams=agent_teams,
        matching_codex_count=len(codex_definitions),
        imported_codex=imported_codex,
        current_imported_codex=current_imported_codex,
        historical_codex=historical_codex,
        stale_codex=stale_codex,
        custom_codex=custom_codex,
        missing_profile_keys=missing_profile_keys,
        codex_contract_coverage=codex_contract_coverage,
        codex_schedule_counts=codex_schedule_counts,
        quality_monitor=quality_monitor,
        quality_scorecard=quality_scorecard,
        runtime_queue=runtime_queue,
        operator_inbox=operator_inbox,
        local_model=local_model,
        worktree_enabled_count=len(worktree_enabled),
        merge_enabled_count=len(merge_enabled),
    )
    coding_quality_bar = _agent_coding_quality_bar_payload(
        local_model=local_model,
        quality_monitor=quality_monitor,
        capability_audit=capability_audit,
        codex_alignment=codex_alignment,
        runtime_queue=runtime_queue,
        operator_inbox=operator_inbox,
    )
    checks = [
        _readiness_check(
            AGENT_OS_READINESS_CHECK_REPO,
            AGENT_OS_READINESS_CHECK_PASSED if repo_ready else AGENT_OS_READINESS_CHECK_FAILED,
            "Repo wiring",
            (
                f"Using {runtime_path}."
                if repo_ready
                else "Selected repo is not reachable from this runtime."
            ),
        ),
        _readiness_check(
            AGENT_OS_READINESS_CHECK_LOCAL_MODEL,
            str(local_model["status"]),
            "Local coding model",
            str(local_model["detail"]),
            count=len(local_model.get("installed_models") or []),
        ),
        _readiness_check(
            AGENT_OS_READINESS_CHECK_QUALITY_GOVERNANCE,
            str(quality_scorecard["status"]),
            "Quality governance",
            str(quality_scorecard["detail"]),
            count=int(quality_scorecard.get("recent_run_count") or 0),
        ),
        _readiness_check(
            AGENT_OS_READINESS_CHECK_RUNTIME_QUEUE,
            str(runtime_queue["status"]),
            "Runtime queue",
            str(runtime_queue["detail"]),
            count=int(runtime_queue.get("open_count") or 0),
        ),
        _readiness_check(
            AGENT_OS_READINESS_CHECK_OPERATOR_INBOX,
            str(operator_inbox["status"]),
            "Operator inbox",
            str(operator_inbox["detail"]),
            count=int(operator_inbox.get("total_action_count") or 0),
        ),
        _readiness_check(
            AGENT_OS_READINESS_CHECK_CODEX_ALIGNMENT,
            str(codex_alignment["status"]),
            "Local-vs-Codex alignment",
            str(codex_alignment["detail"]),
            count=int(codex_alignment.get("reference_count") or 0),
        ),
        _readiness_check(
            AGENT_OS_READINESS_CHECK_PROFILES,
            AGENT_OS_READINESS_CHECK_PASSED if profiles else AGENT_OS_READINESS_CHECK_FAILED,
            "Agent bench",
            f"{len(profiles)} repo agents are available.",
            count=len(profiles),
        ),
        _readiness_check(
            AGENT_OS_READINESS_CHECK_TEAMS,
            AGENT_OS_READINESS_CHECK_PASSED if agent_teams else AGENT_OS_READINESS_CHECK_WARNING,
            "Agent supervision",
            (
                f"{len(agent_teams)} supervision team(s) can coordinate repo work."
                if agent_teams
                else "No macro supervision teams are available yet."
            ),
            count=len(agent_teams),
        ),
        _readiness_check(
            AGENT_OS_READINESS_CHECK_CODEX,
            AGENT_OS_READINESS_CHECK_PASSED
            if not codex_definitions or not (codex_keys - profile_keys)
            else AGENT_OS_READINESS_CHECK_WARNING,
            "Codex automation parity",
            (
                (
                    f"Imported all {len(codex_definitions)} current Codex automation(s); "
                    f"{len(historical_codex)} historical imported profile(s) retained for audit."
                )
                if codex_definitions
                else "No matching local Codex automations were found for this repo."
            ),
            count=len(current_imported_codex),
        ),
        _readiness_check(
            AGENT_OS_READINESS_CHECK_CODEX_FRESHNESS,
            AGENT_OS_READINESS_CHECK_PASSED if not stale_codex else AGENT_OS_READINESS_CHECK_WARNING,
            "Codex prompt freshness",
            (
                "Imported Codex prompt snapshots match the current local automation files."
                if not stale_codex
                else f"{len(stale_codex)} imported Codex profile(s) need prompt or schedule resync."
            ),
            count=len(stale_codex),
        ),
        _readiness_check(
            AGENT_OS_READINESS_CHECK_CODEX_CONTRACTS,
            (
                AGENT_OS_READINESS_CHECK_PASSED
                if not imported_codex
                or codex_contract_coverage["missing_workspace_count"] == 0
                else AGENT_OS_READINESS_CHECK_WARNING
            ),
            "Codex operating contracts",
            (
                "No imported Codex automations need operating contracts."
                if not imported_codex
                else (
                    f"All {codex_contract_coverage['total']} imported Codex agent(s) expose "
                    f"workspace contracts; {codex_contract_coverage['d_drive_aligned_count']} "
                    f"D-drive aligned, {codex_contract_coverage['key_command_profile_count']} "
                    f"with command evidence, {codex_contract_coverage['safety_boundary_profile_count']} "
                    "with explicit safety boundaries."
                    if codex_contract_coverage["missing_workspace_count"] == 0
                    else (
                        f"{codex_contract_coverage['missing_workspace_count']} imported Codex "
                        "agent(s) are missing a workspace contract."
                    )
                )
            ),
            count=codex_contract_coverage["workspace_count"],
        ),
        _readiness_check(
            AGENT_OS_READINESS_CHECK_SAFE_DEFAULTS,
            AGENT_OS_READINESS_CHECK_PASSED
            if not worktree_enabled and not merge_enabled
            else AGENT_OS_READINESS_CHECK_WARNING,
            "Mutation safety",
            (
                "No agent has worktree or merge permission enabled by default."
                if not worktree_enabled and not merge_enabled
                else (
                    f"{len(worktree_enabled)} agents can patch and "
                    f"{len(merge_enabled)} agents can merge; safety gates still apply."
                )
            ),
        ),
        _readiness_check(
            AGENT_OS_READINESS_CHECK_SCHEDULES,
            AGENT_OS_READINESS_CHECK_PASSED,
            "Scheduled agents",
            (
                (
                    f"{len(always_on_schedules)} always-on runtime(s) and "
                    f"{len(active_schedules) - len(always_on_schedules)} scheduled runtime(s) are active; "
                    "always-on agents queue the next safe cycle after open work clears and rest "
                    f"{AGENT_RUNTIME_DEFAULT_REST_MINUTES} minutes after "
                    f"{AGENT_RUNTIME_DEFAULT_WORK_WINDOW_MINUTES // 60} hours."
                )
                if always_on_schedules
                else (
                    f"{len(active_schedules)} schedules are active; paused agents stay dormant."
                    if active_schedules
                    else "All schedules are paused or disabled until the operator enables them."
                )
            ),
            count=len(active_schedules),
        ),
        _readiness_check(
            AGENT_OS_READINESS_CHECK_QUESTIONS,
            AGENT_OS_READINESS_CHECK_PASSED
            if pending_questions == 0
            else AGENT_OS_READINESS_CHECK_WARNING,
            "Operator questions",
            (
                "No agent is waiting on operator input."
                if pending_questions == 0
                else f"{pending_questions} operator question(s) need attention."
            ),
            count=pending_questions,
        ),
    ]
    failed = sum(1 for check in checks if check["status"] == AGENT_OS_READINESS_CHECK_FAILED)
    warnings = sum(1 for check in checks if check["status"] == AGENT_OS_READINESS_CHECK_WARNING)
    passed = len(checks) - failed - warnings
    score = round((passed / max(len(checks), 1)) * 100)
    return {
        "status": (
            AGENT_OS_READINESS_READY
            if failed == 0 and warnings == 0
            else AGENT_OS_READINESS_NEEDS_ATTENTION
        ),
        "score": score,
        "passed": passed,
        "warnings": warnings,
        "failed": failed,
        "checks": checks,
        "repo": {
            "id": repo.id,
            "name": repo.name,
            "path": repo.path,
            "resolved_path": str(runtime_path) if runtime_path is not None else None,
            "preferred_for_autopilot": cb_indexer.is_preferred_autopilot_repo(repo),
        },
        "agents": {
            "total": len(profiles),
            "macro": sum(1 for profile in profiles if profile.get("tier") == AGENT_PROFILE_TIER_MACRO),
            "micro": sum(1 for profile in profiles if profile.get("tier") == AGENT_PROFILE_TIER_MICRO),
            "specialist": sum(1 for profile in profiles if profile.get("tier") == AGENT_PROFILE_TIER_SPECIALIST),
            "codex_automation": len(imported_codex),
            "current_codex_automation": len(current_imported_codex),
            "historical_codex_automation": len(historical_codex),
            "codex_custom_overrides": len(custom_codex),
            "active_schedules": len(active_schedules),
            "always_on_schedules": len(always_on_schedules),
            "pending_questions": pending_questions,
        },
        "teams": agent_teams,
        "local_model": local_model,
        "quality_scorecard": quality_scorecard,
        "runtime_queue": runtime_queue,
        "operator_inbox": operator_inbox,
        "codex_alignment": codex_alignment,
        AGENT_CODEX_BENCH_KEY: codex_bench,
        AGENT_QUALITY_MONITOR_KEY: quality_monitor,
        AGENT_OS_CAPABILITY_AUDIT_KEY: capability_audit,
        AGENT_CODING_QUALITY_BAR_KEY: coding_quality_bar,
        "codex_automations": {
            "matching": len(codex_definitions),
            "imported": len(imported_codex),
            "current_imported": len(current_imported_codex),
            "historical_imported": len(historical_codex),
            "missing_profile_keys": missing_profile_keys,
            "historical_profile_keys": sorted(str(profile.get("profile_key") or "") for profile in historical_codex),
            "stale_profile_keys": sorted(str(profile.get("profile_key") or "") for profile in stale_codex),
            "custom_override_keys": sorted(str(profile.get("profile_key") or "") for profile in custom_codex),
            "schedule_mirror": codex_schedule_counts,
            "contract_coverage": codex_contract_coverage,
            "profiles": codex_profile_matrix,
        },
    }


def _get_agent_profile(
    db: Session,
    profile_id: int | None,
    *,
    user_id: int | None,
) -> ProjectAutonomyAgentProfile | None:
    if profile_id is None:
        return None
    q = db.query(ProjectAutonomyAgentProfile).filter(ProjectAutonomyAgentProfile.id == int(profile_id))
    if user_id is None:
        q = q.filter(ProjectAutonomyAgentProfile.user_id.is_(None))
    else:
        q = q.filter(ProjectAutonomyAgentProfile.user_id == user_id)
    return q.first()


def _agent_schedule_rrule_parts(rrule: str | None) -> dict[str, str]:
    parts: dict[str, str] = {}
    normalized = _normalize_agent_schedule_rrule(rrule)
    for chunk in normalized.split(AGENT_SCHEDULE_RRULE_SEPARATOR):
        if AGENT_SCHEDULE_RRULE_ASSIGN not in chunk:
            continue
        key, value = chunk.split(AGENT_SCHEDULE_RRULE_ASSIGN, 1)
        clean_key = key.strip().upper()
        clean_value = value.strip().upper()
        if clean_key and clean_value:
            parts[clean_key] = clean_value
    return parts


def _agent_schedule_interval(rrule: str | None) -> timedelta | None:
    parts = _agent_schedule_rrule_parts(rrule)
    freq = parts.get(AGENT_SCHEDULE_RRULE_FREQ_KEY)
    if not freq:
        return None
    try:
        interval = int(parts.get(AGENT_SCHEDULE_RRULE_INTERVAL_KEY) or AGENT_SCHEDULE_DEFAULT_INTERVAL)
    except (TypeError, ValueError):
        interval = AGENT_SCHEDULE_DEFAULT_INTERVAL
    interval = max(AGENT_SCHEDULE_MIN_INTERVAL, interval)
    if freq == AGENT_SCHEDULE_RRULE_FREQ_MINUTELY:
        return timedelta(minutes=min(interval, AGENT_SCHEDULE_MAX_INTERVAL_MINUTES))
    if freq == AGENT_SCHEDULE_RRULE_FREQ_HOURLY:
        return timedelta(hours=min(interval, AGENT_SCHEDULE_MAX_INTERVAL_HOURS))
    return None


def _agent_schedule_config_for_profile(profile: ProjectAutonomyAgentProfile) -> dict[str, Any]:
    schedule_config = _json_load(profile.schedule_json, {})
    if not isinstance(schedule_config, dict):
        schedule_config = {}
    config = _default_agent_schedule_config()
    config.update(schedule_config)
    budget = config.get("budget")
    config["budget"] = (
        dict(budget)
        if isinstance(budget, dict)
        else dict(DEFAULT_AGENT_SCHEDULE["budget"])
    )
    return config


def _agent_runtime_mode(schedule_config: Mapping[str, Any]) -> str:
    mode = str(schedule_config.get(AGENT_RUNTIME_MODE_KEY) or "").strip().lower()
    if mode == AGENT_RUNTIME_MODE_ALWAYS_ON:
        return AGENT_RUNTIME_MODE_ALWAYS_ON
    return AGENT_RUNTIME_MODE_SCHEDULED


def _agent_runtime_is_always_on(schedule_config: Mapping[str, Any]) -> bool:
    return _agent_runtime_mode(schedule_config) == AGENT_RUNTIME_MODE_ALWAYS_ON


def _agent_runtime_minutes(
    schedule_config: Mapping[str, Any],
    key: str,
    default: int,
) -> int:
    try:
        value = int(schedule_config.get(key) or default)
    except (TypeError, ValueError):
        value = default
    return max(1, value)


def _parse_agent_runtime_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        return parsed.replace(tzinfo=None)
    return parsed


def _agent_runtime_rest_gate(
    profile: ProjectAutonomyAgentProfile,
    schedule: ProjectAutonomyAgentSchedule,
    *,
    now: datetime,
) -> dict[str, Any] | None:
    schedule_config = _agent_schedule_config_for_profile(profile)
    if not _agent_runtime_is_always_on(schedule_config):
        return None
    rest_until = _parse_agent_runtime_datetime(schedule_config.get(AGENT_RUNTIME_REST_UNTIL_KEY))
    if rest_until is not None and rest_until > now:
        schedule.next_run_at = rest_until
        schedule.updated_at = now
        return {
            "profile_id": profile.id,
            "profile_key": profile.profile_key,
            "reason": AGENT_SCHEDULE_SKIP_RUNTIME_REST,
            "rest_until": rest_until.isoformat(),
            "next_run_at": rest_until.isoformat(),
        }
    work_started = _parse_agent_runtime_datetime(schedule_config.get(AGENT_RUNTIME_WORK_STARTED_AT_KEY))
    if work_started is None or (rest_until is not None and rest_until <= now):
        schedule_config[AGENT_RUNTIME_WORK_STARTED_AT_KEY] = now.isoformat()
        schedule_config[AGENT_RUNTIME_REST_UNTIL_KEY] = None
        profile.schedule_json = _json_text(schedule_config)
        return None
    work_window = timedelta(
        minutes=_agent_runtime_minutes(
            schedule_config,
            AGENT_RUNTIME_WORK_WINDOW_MINUTES_KEY,
            AGENT_RUNTIME_DEFAULT_WORK_WINDOW_MINUTES,
        )
    )
    if now - work_started < work_window:
        return None
    rest_until = now + timedelta(
        minutes=_agent_runtime_minutes(
            schedule_config,
            AGENT_RUNTIME_REST_MINUTES_KEY,
            AGENT_RUNTIME_DEFAULT_REST_MINUTES,
        )
    )
    schedule_config[AGENT_RUNTIME_WORK_STARTED_AT_KEY] = None
    schedule_config[AGENT_RUNTIME_REST_UNTIL_KEY] = rest_until.isoformat()
    profile.schedule_json = _json_text(schedule_config)
    schedule.next_run_at = rest_until
    schedule.updated_at = now
    return {
        "profile_id": profile.id,
        "profile_key": profile.profile_key,
        "reason": AGENT_SCHEDULE_SKIP_RUNTIME_REST,
        "rest_until": rest_until.isoformat(),
        "next_run_at": rest_until.isoformat(),
    }


def _next_agent_schedule_run_at(
    rrule: str | None,
    *,
    from_time: datetime | None = None,
) -> datetime | None:
    interval = _agent_schedule_interval(rrule)
    if interval is None:
        return None
    return (from_time or _utcnow()) + interval


def _ensure_agent_schedule_row(
    db: Session,
    profile: ProjectAutonomyAgentProfile,
) -> ProjectAutonomyAgentSchedule:
    schedule_config = _agent_schedule_config_for_profile(profile)
    schedule = (
        db.query(ProjectAutonomyAgentSchedule)
        .filter(ProjectAutonomyAgentSchedule.profile_id == profile.id)
        .order_by(ProjectAutonomyAgentSchedule.id.desc())
        .first()
    )
    if schedule is None:
        schedule = ProjectAutonomyAgentSchedule(profile_id=profile.id)
        db.add(schedule)
        db.flush()
    if schedule.rrule is None and schedule_config.get("rrule") is not None:
        schedule.rrule = str(schedule_config.get("rrule"))
    if not schedule.budget_json or schedule.budget_json == "{}":
        schedule.budget_json = _json_text(
            schedule_config.get("budget") or DEFAULT_AGENT_SCHEDULE["budget"]
        )
    return schedule


def _sync_agent_schedule_row(
    db: Session,
    profile: ProjectAutonomyAgentProfile,
    *,
    now: datetime | None = None,
) -> ProjectAutonomyAgentSchedule:
    schedule = _ensure_agent_schedule_row(db, profile)
    schedule_config = _agent_schedule_config_for_profile(profile)
    if "rrule" in schedule_config:
        schedule.rrule = schedule_config.get("rrule")
    if "budget" in schedule_config:
        schedule.budget_json = _json_text(
            schedule_config.get("budget") or DEFAULT_AGENT_SCHEDULE["budget"]
        )
    active = (
        profile.status == AGENT_PROFILE_STATUS_ACTIVE
        and bool(profile.schedule_enabled)
        and (
            _agent_schedule_interval(schedule.rrule) is not None
            or _agent_runtime_is_always_on(schedule_config)
        )
    )
    schedule.status = AGENT_SCHEDULE_STATUS_ACTIVE if active else AGENT_SCHEDULE_STATUS_PAUSED
    if active:
        if schedule.next_run_at is None:
            if _agent_runtime_is_always_on(schedule_config):
                schedule.next_run_at = now or _utcnow()
            else:
                schedule.next_run_at = _next_agent_schedule_run_at(schedule.rrule, from_time=now)
    else:
        schedule.next_run_at = None
    schedule.updated_at = now or _utcnow()
    return schedule


def _mark_agent_schedule_cycle_started(
    db: Session,
    profile: ProjectAutonomyAgentProfile,
    *,
    now: datetime | None = None,
) -> ProjectAutonomyAgentSchedule:
    started_at = now or _utcnow()
    schedule = _sync_agent_schedule_row(db, profile, now=started_at)
    schedule_config = _agent_schedule_config_for_profile(profile)
    schedule.last_run_at = started_at
    schedule.next_run_at = (
        started_at
        if _agent_runtime_is_always_on(schedule_config)
        else _next_agent_schedule_run_at(schedule.rrule, from_time=started_at)
    )
    schedule.updated_at = started_at
    return schedule


def _profile_has_open_scheduled_cycle(db: Session, profile_id: int) -> bool:
    return (
        db.query(ProjectAutonomyRun.id)
        .filter(
            ProjectAutonomyRun.agent_profile_id == int(profile_id),
            ProjectAutonomyRun.autonomy_level == AUTONOMY_LEVEL_SCHEDULED_AGENT,
            ProjectAutonomyRun.archived_at.is_(None),
            ~ProjectAutonomyRun.status.in_(list(TERMINAL_STATUSES)),
        )
        .first()
        is not None
    )


def _default_agent_profile_for_repo(
    db: Session,
    repo: CodeRepo,
    *,
    user_id: int | None,
) -> ProjectAutonomyAgentProfile | None:
    bootstrap_agent_profiles(db, repo_id=int(repo.id), user_id=user_id)
    return (
        _agent_profile_query(db, user_id=user_id, repo_id=int(repo.id))
        .filter(ProjectAutonomyAgentProfile.profile_key == PM_COORDINATOR_PROFILE_KEY)
        .first()
    )


def create_run(
    db: Session,
    *,
    prompt: str,
    repo_id: int | None = None,
    user_id: int | None = None,
    agent_profile_id: int | None = None,
    parent_run_id: str | None = None,
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
    agent_profile = _get_agent_profile(db, agent_profile_id, user_id=user_id)
    if agent_profile_id is not None and agent_profile is None:
        raise ValueError("Agent profile not found.")
    if repo_id is None and agent_profile is not None:
        repo_id = int(agent_profile.repo_id)
    repo = _resolve_repo_for_run(db, repo_id, user_id=user_id)
    if repo is None:
        raise ValueError("No reachable registered repo is available for Project Autopilot.")
    repo = _canonical_repo_for_autopilot(db, repo, user_id=user_id)
    if resolve_repo_runtime_path(repo) is None:
        raise ValueError("The selected repo is registered but not reachable from this runtime.")
    agent_profile = _canonical_agent_profile_for_repo(
        db,
        agent_profile,
        repo,
        user_id=user_id,
    )
    if agent_profile is not None and int(agent_profile.repo_id) != int(repo.id):
        raise ValueError("Agent profile does not belong to the selected repo.")
    if agent_profile is None:
        agent_profile = _default_agent_profile_for_repo(db, repo, user_id=user_id)
    if agent_profile is not None:
        model_policy = agent_profile.model_policy or model_policy
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
            "agent": _agent_profile_snapshot(agent_profile),
        },
    )
    row = ProjectAutonomyRun(
        run_id=run_id,
        project_run_id=project_run.id,
        user_id=user_id,
        repo_id=int(repo.id),
        agent_profile_id=agent_profile.id if agent_profile is not None else None,
        parent_run_id=(parent_run_id or None),
        agent_snapshot_json=_json_text(_agent_profile_snapshot(agent_profile)),
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
        detail={
            "repo_id": int(repo.id),
            "repo_name": repo.name,
            "agent": _agent_profile_snapshot(agent_profile),
        },
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
        "agent": _agent_profile_snapshot(agent_profile),
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
    slash_command = (
        None
        if start_planning or clean_attachments
        else _parse_autopilot_slash_command(clean_prompt)
    )
    if not start_planning and slash_command is not None:
        command, args = slash_command
        _handle_autopilot_slash_command(
            db,
            row,
            command=command,
            args=args,
            user_id=user_id,
        )
    elif not start_planning:
        _record_message(
            db,
            row,
            "assistant",
            _initial_chat_reply(clean_prompt),
            message_type="chat",
            metadata={
                "repo_id": int(repo.id),
                "repo_name": repo.name,
                "agent": _agent_profile_snapshot(agent_profile),
            },
            commit=False,
        )
    db.commit()
    db.refresh(row)
    return row


def _cancel_run_row(db: Session, row: ProjectAutonomyRun) -> None:
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
    elif row.status in ACTIVE_STATUSES:
        _record_step(
            db,
            row,
            row.current_stage or "cancel",
            "Cancel requested",
            status="completed",
            detail={"requested_at": _utcnow().isoformat()},
            commit=False,
        )
        _record_message(
            db,
            row,
            "assistant",
            "Cancellation requested. I will stop this run at the next safe checkpoint.",
            message_type="status",
            commit=False,
        )


def request_cancel(db: Session, run_id: str, *, user_id: int | None = None) -> dict[str, Any] | None:
    row = _get_run_row(db, run_id, user_id=user_id)
    if row is None:
        return None
    _cancel_run_row(db, row)
    db.commit()
    db.refresh(row)
    return run_payload(db, row, include_events=True)


def merge_run(db: Session, run_id: str, *, user_id: int | None = None) -> dict[str, Any] | None:
    row = _get_run_row(db, run_id, user_id=user_id)
    if row is None:
        return None
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


def _parse_autopilot_slash_command(content: str) -> tuple[str, str] | None:
    clean = (content or "").strip()
    if not clean.startswith(AUTOPILOT_SLASH_PREFIX):
        return None
    body = clean[len(AUTOPILOT_SLASH_PREFIX):].strip()
    if not body:
        return AUTOPILOT_COMMAND_HELP, ""
    parts = body.split(maxsplit=1)
    command = parts[0].strip().lower()
    args = parts[1].strip() if len(parts) > 1 else ""
    return command, args


def _format_profile_schedule(profile_payload: dict[str, Any]) -> str:
    schedule = profile_payload.get("schedule")
    if not isinstance(schedule, dict):
        schedule = {}
    status = str(schedule.get("status") or AGENT_SCHEDULE_STATUS_PAUSED)
    enabled = "enabled" if profile_payload.get("schedule_enabled") else "disabled"
    runtime_mode = str(schedule.get(AGENT_RUNTIME_MODE_KEY) or AGENT_RUNTIME_MODE_SCHEDULED)
    if runtime_mode == AGENT_RUNTIME_MODE_ALWAYS_ON:
        rest_until = str(schedule.get(AGENT_RUNTIME_REST_UNTIL_KEY) or "").strip()
        rest = f", resting until {rest_until}" if rest_until else ""
        return f"enabled, {status}, always-on queue{rest}"
    cadence = str(schedule.get("cadence") or schedule.get("rrule") or "manual")
    next_run = str(schedule.get("next_run_at") or "not scheduled")
    return f"{enabled}, {status}, cadence {cadence}, next {next_run}"


def _profile_codex_source_status(profile: ProjectAutonomyAgentProfile) -> str:
    prompt_setting = _json_load(profile.prompt_setting_json, {})
    if not isinstance(prompt_setting, dict) or prompt_setting.get("source") != CODEX_AUTOMATION_SOURCE:
        return ""
    automation = prompt_setting.get("codex_automation")
    status = ""
    if isinstance(automation, dict):
        status = str(automation.get("status") or "").strip()
    if not status:
        schedule = _json_load(profile.schedule_json, {})
        if isinstance(schedule, dict):
            status = str(schedule.get("source_status") or "").strip()
    return status.upper()


def _repo_codex_agent_profiles(
    db: Session,
    *,
    repo_id: int,
    user_id: int | None,
) -> list[ProjectAutonomyAgentProfile]:
    bootstrap_agent_profiles(db, repo_id=repo_id, user_id=user_id)
    return [
        profile
        for profile in _agent_profile_query(db, user_id=user_id, repo_id=repo_id).all()
        if _profile_codex_source_status(profile)
    ]


def _codex_schedule_summary(profiles: Iterable[Mapping[str, Any]]) -> str:
    counts = _codex_schedule_counts(profiles)
    return (
        "Codex schedule mirror: "
        f"{counts['source_active_enabled']} of {counts['source_active']} source-active "
        "Codex automation schedules are enabled in CHILI "
        f"({counts['source_active_always_on']} always-on, {counts['source_active_scheduled']} scheduled). "
        f"{counts['source_active_disabled']} source-active schedules remain paused here; "
        "permissions stay observe/research/plan unless you explicitly enable patching."
    )


def set_codex_agent_schedules(
    db: Session,
    *,
    repo_id: int,
    user_id: int | None = None,
    enable_source_active: bool,
    always_on: bool = False,
) -> dict[str, Any]:
    """Enable source-active Codex mirrors or pause all imported Codex mirrors atomically."""
    profiles = _repo_codex_agent_profiles(db, repo_id=repo_id, user_id=user_id)
    changed = 0
    if enable_source_active:
        for profile in profiles:
            if _profile_codex_source_status(profile) != "ACTIVE":
                continue
            changed += 1
            profile.status = AGENT_PROFILE_STATUS_ACTIVE
            profile.schedule_enabled = True
            profile.schedule_json = _json_text(
                _codex_profile_always_on_schedule_config(profile)
                if always_on
                else _codex_profile_source_schedule_config(profile)
            )
            _sync_agent_schedule_row(db, profile)
    else:
        for profile in profiles:
            changed += 1
            profile.status = AGENT_PROFILE_STATUS_PAUSED
            profile.schedule_enabled = False
            schedule_config = _codex_profile_source_schedule_config(profile)
            schedule_config["enabled"] = False
            profile.schedule_json = _json_text(schedule_config)
            _sync_agent_schedule_row(db, profile)
    db.commit()
    payloads = list_agent_profiles(db, repo_id=repo_id, user_id=user_id)
    action = "Enabled" if enable_source_active else "Paused"
    target = (
        (
            "source-active Codex agents as always-on queues"
            if always_on
            else "source-active Codex schedules"
        )
        if enable_source_active
        else "Codex automation schedules"
    )
    return {
        "changed": changed,
        "enabled": bool(enable_source_active),
        "always_on": bool(always_on and enable_source_active),
        "message": (
            f"{action} {changed} {target} in CHILI. "
            "They remain plan-only unless you separately enable worktree patches."
            if enable_source_active
            else f"{action} {changed} {target} in CHILI."
        ),
        "schedule_mirror": _codex_schedule_counts(payloads),
        "summary": _codex_schedule_summary(payloads),
        "agents": payloads,
    }


def adopt_codex_agent_system(
    db: Session,
    *,
    repo_id: int,
    user_id: int | None = None,
    wake_now: bool = True,
    limit: int = AGENT_SCHEDULE_DUE_CYCLE_LIMIT,
) -> dict[str, Any]:
    """Mirror Codex automations into CHILI's safe always-on Agent OS loop."""
    sync_payload = sync_codex_agent_profiles(db, repo_id=repo_id, user_id=user_id)
    schedule_payload = set_codex_agent_schedules(
        db,
        repo_id=repo_id,
        user_id=user_id,
        enable_source_active=True,
        always_on=True,
    )
    wake_payload: dict[str, Any] = {
        "started": 0,
        "skipped": [],
        "runs": [],
        "checked": 0,
        "woken": 0,
        "codex_only": True,
        "repo_id": int(repo_id),
        "message": "Codex always-on queues were enabled; no immediate wake was requested.",
    }
    if wake_now:
        wake_payload = run_agent_schedules_now(
            db,
            repo_id=repo_id,
            user_id=user_id,
            codex_only=True,
            limit=limit,
        )
    readiness = agent_os_readiness(db, repo_id=repo_id, user_id=user_id)
    sync_summary = {
        "source_count": int(sync_payload.get("source_count") or 0),
        "imported_count": int(sync_payload.get("imported_count") or 0),
        "current_count": int(sync_payload.get("current_count") or 0),
        "created_count": int(sync_payload.get("created_count") or 0),
        "refreshed_count": int(sync_payload.get("refreshed_count") or 0),
        "custom_count": int(sync_payload.get("custom_count") or 0),
        "historical_count": int(sync_payload.get("historical_count") or 0),
        "stale_count": int(sync_payload.get("stale_count") or 0),
    }
    started = int(wake_payload.get("started") or 0)
    enabled = int(schedule_payload.get("schedule_mirror", {}).get("source_active_always_on") or 0)
    return {
        "message": (
            f"Adopted {enabled} source-active Codex agent(s) as CHILI always-on plan-only queues"
            f" and started {started} bounded cycle(s)."
            if wake_now
            else f"Adopted {enabled} source-active Codex agent(s) as CHILI always-on plan-only queues."
        ),
        "sync": sync_summary,
        "schedule": schedule_payload,
        "wake": wake_payload,
        "readiness": readiness,
        "agents": schedule_payload.get("agents") or [],
    }


def _autopilot_command_codex_schedule(
    db: Session,
    row: ProjectAutonomyRun,
    *,
    user_id: int | None,
    action: str,
) -> str:
    if row.repo_id is None:
        return "This run is not attached to a repo, so I cannot inspect Codex schedules."
    profiles = _repo_codex_agent_profiles(db, repo_id=int(row.repo_id), user_id=user_id)
    if action == AUTOPILOT_COMMAND_SCHEDULE_CODEX_ACTIVE:
        payload = set_codex_agent_schedules(
            db,
            repo_id=int(row.repo_id),
            user_id=user_id,
            enable_source_active=True,
        )
        return f"{payload['message']}\n{payload['summary']}"
    if action == AUTOPILOT_COMMAND_SCHEDULE_CODEX_ALWAYS_ON:
        payload = set_codex_agent_schedules(
            db,
            repo_id=int(row.repo_id),
            user_id=user_id,
            enable_source_active=True,
            always_on=True,
        )
        return f"{payload['message']}\n{payload['summary']}"
    if action == AUTOPILOT_COMMAND_SCHEDULE_CODEX_ADOPT:
        payload = adopt_codex_agent_system(
            db,
            repo_id=int(row.repo_id),
            user_id=user_id,
        )
        wake = _mapping_payload(payload.get("wake"))
        return (
            f"{payload['message']}\n"
            f"{payload['schedule']['summary']}\n"
            f"Wake result: {int(wake.get('started') or 0)} started, "
            f"{len(wake.get('skipped') or [])} skipped."
        )
    if action == AUTOPILOT_COMMAND_SCHEDULE_CODEX_PAUSE:
        payload = set_codex_agent_schedules(
            db,
            repo_id=int(row.repo_id),
            user_id=user_id,
            enable_source_active=False,
        )
        return f"{payload['message']}\n{payload['summary']}"
    payloads = list_agent_profiles(db, repo_id=int(row.repo_id), user_id=user_id)
    lines = [_codex_schedule_summary(payloads)]
    for profile in payloads:
        prompt_setting = _mapping_payload(profile.get("prompt_setting"))
        if prompt_setting.get("source") != CODEX_AUTOMATION_SOURCE:
            continue
        schedule = _mapping_payload(profile.get("schedule"))
        source_status = str(schedule.get("source_status") or "UNKNOWN").strip()
        enabled = "enabled" if profile.get("schedule_enabled") else "paused"
        lines.append(
            "- "
            f"{profile.get('name') or profile.get('profile_key')}: "
            f"source {source_status}, CHILI {enabled}, {_format_profile_schedule(dict(profile))}."
        )
    return "\n".join(lines)


def _agent_profile_for_run(
    db: Session,
    row: ProjectAutonomyRun,
) -> ProjectAutonomyAgentProfile | None:
    if row.agent_profile_id is None:
        return None
    return db.get(ProjectAutonomyAgentProfile, int(row.agent_profile_id))


def _autopilot_command_help() -> str:
    return "Autopilot commands:\n" + "\n".join(f"- {line}" for line in AUTOPILOT_SLASH_HELP_LINES)


def _autopilot_command_status(
    db: Session,
    row: ProjectAutonomyRun,
) -> str:
    profile = _agent_profile_for_run(db, row)
    profile_name = profile.name if profile is not None else "Default agent"
    profile_status = profile.status if profile is not None else "unbound"
    pending_questions = (
        db.query(ProjectAutonomyOperatorQuestion)
        .filter(
            ProjectAutonomyOperatorQuestion.run_id == row.run_id,
            ProjectAutonomyOperatorQuestion.status == OPERATOR_QUESTION_STATUS_PENDING,
        )
        .count()
    )
    plan_state = row.plan_status or "unknown"
    merge_state = row.merge_status or "pending"
    stage = row.current_stage or row.status or "unknown"
    return (
        f"{profile_name} is {profile_status}. Run status is {row.status}; "
        f"stage {stage}; plan {plan_state}; merge {merge_state}. "
        f"Pending operator questions: {pending_questions}."
    )


def _autopilot_changed_files_reply(
    db: Session,
    row: ProjectAutonomyRun,
) -> str:
    files = [str(item) for item in _json_load(row.files_json, []) if str(item or "").strip()]
    if not files:
        diff_artifacts = (
            db.query(ProjectAutonomyArtifact)
            .filter(
                ProjectAutonomyArtifact.run_id == row.run_id,
                ProjectAutonomyArtifact.artifact_type == "diff",
            )
            .order_by(ProjectAutonomyArtifact.id.asc())
            .limit(20)
            .all()
        )
        files = [str(artifact.name) for artifact in diff_artifacts if str(artifact.name or "").strip()]
    if not files:
        return "No changed files are recorded for this run yet."
    visible = files[:8]
    suffix = f"\n...and {len(files) - len(visible)} more." if len(files) > len(visible) else ""
    return "Changed files recorded for this run:\n" + "\n".join(f"- {path}" for path in visible) + suffix


def _validation_command_label(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, Mapping):
        for key in ("command", "cmd", "name", "label"):
            value = str(item.get(key) or "").strip()
            if value:
                return value
    return ""


def _autopilot_validation_reply(
    db: Session,
    row: ProjectAutonomyRun,
) -> str:
    commands = [
        label
        for item in _json_load(row.validation_json, [])
        if (label := _validation_command_label(item))
    ]
    latest = _latest_artifact_json(
        db,
        row.run_id,
        artifact_type="validation",
        name="validation_results",
    )
    if not commands and isinstance(latest.get("commands"), list):
        commands = [_validation_command_label(item) for item in latest.get("commands") or []]
        commands = [cmd for cmd in commands if cmd]
    status = str(latest.get("status") or latest.get("result") or "").strip()
    if not commands and not status:
        return "No validation or test results are recorded for this run yet."
    lines = ["Validation recorded for this run:"]
    if status:
        lines.append(f"- Status: {status}")
    for command in commands[:8]:
        lines.append(f"- {command}")
    if len(commands) > 8:
        lines.append(f"...and {len(commands) - 8} more.")
    return "\n".join(lines)


def _autopilot_evidence_reply(
    db: Session,
    row: ProjectAutonomyRun,
) -> str:
    artifacts = (
        db.query(ProjectAutonomyArtifact)
        .filter(ProjectAutonomyArtifact.run_id == row.run_id)
        .order_by(ProjectAutonomyArtifact.id.desc())
        .limit(8)
        .all()
    )
    if not artifacts:
        return "No artifacts or evidence are recorded for this run yet."
    lines = ["Latest run artifacts:"]
    for artifact in artifacts:
        size = f" ({artifact.byte_length} bytes)" if int(artifact.byte_length or 0) else ""
        lines.append(f"- {artifact.artifact_type}: {artifact.name}{size}")
    return "\n".join(lines)


def _autopilot_command_agents(
    db: Session,
    row: ProjectAutonomyRun,
    *,
    user_id: int | None,
) -> str:
    agents = list_agent_profiles(db, repo_id=row.repo_id, user_id=user_id)
    if not agents:
        return "No repo agents are configured yet."
    lines = ["Repo agents:"]
    for agent in agents:
        schedule = _format_profile_schedule(agent)
        pending = int(agent.get("pending_question_count") or 0)
        suffix = f"; {pending} pending question{'s' if pending != 1 else ''}" if pending else ""
        lines.append(
            "- "
            f"{agent.get('name') or agent.get('profile_key')} "
            f"({agent.get('tier')}, {agent.get('role')}) - "
            f"{agent.get('status')}; schedule {schedule}{suffix}."
        )
    return "\n".join(lines)


def _autopilot_command_model(
    db: Session,
    row: ProjectAutonomyRun,
) -> str:
    profile = _agent_profile_for_run(db, row)
    local_model = _local_model_readiness_payload(select_local_model())
    model_name = local_model.get("model") or "none"
    model_lines = [
        f"Local model: {model_name}.",
        str(local_model.get("detail") or ""),
    ]
    if not local_model.get("coding_ready"):
        model_lines.append(f"Next step: {local_model.get('recommendation')}")
    skipped = local_model.get("skipped_models")
    if isinstance(skipped, dict) and skipped:
        model_lines.append(
            "Cooling down: "
            + ", ".join(f"{name} ({info.get('reason', 'recent failure')})" for name, info in skipped.items())
        )
    if profile is None:
        return (
            f"This run is using model policy {row.model_policy or 'local_first'}.\n\n"
            + "\n".join(line for line in model_lines if line)
        )
    prompt_setting = _json_load(profile.prompt_setting_json, {})
    if not isinstance(prompt_setting, dict):
        prompt_setting = {}
    source = str(prompt_setting.get("source") or "generated profile")
    system_prompt = str(prompt_setting.get("system_prompt") or "").strip()
    first_line = system_prompt.splitlines()[0][:180] if system_prompt else "No prompt text stored."
    return (
        f"{profile.name} uses model policy {profile.model_policy or 'local_first'} "
        f"from {source}. Prompt: {first_line}\n\n"
        + "\n".join(line for line in model_lines if line)
    )


def _autopilot_doctor_status_label(status: Any) -> str:
    if str(status or "") == AGENT_OS_READINESS_READY:
        return AUTOPILOT_DOCTOR_READY_COPY
    return AUTOPILOT_DOCTOR_NEEDS_ATTENTION_COPY


def _readiness_check_detail(
    readiness: Mapping[str, Any],
    key: str,
    fallback: str,
) -> str:
    checks = readiness.get("checks")
    if not isinstance(checks, list):
        return fallback
    for check in checks:
        if not isinstance(check, Mapping):
            continue
        if str(check.get("key") or "") == key:
            detail = str(check.get("detail") or "").strip()
            return detail or fallback
    return fallback


def _autopilot_coding_quality_bar_lines(readiness: Mapping[str, Any]) -> list[str]:
    bar = _mapping_payload(readiness.get(AGENT_CODING_QUALITY_BAR_KEY))
    if not bar:
        return []
    score = int(bar.get("score") or 0)
    target = int(bar.get("target_score") or AGENT_CODING_QUALITY_BAR_TARGET_SCORE)
    status = str(bar.get("status") or "checking").replace("_", " ")
    detail = str(bar.get("detail") or "").strip()
    next_action_label = str(bar.get("next_action_label") or "").strip()
    next_action_detail = str(bar.get("next_action_detail") or "").strip()
    lines = [f"Codex/Claude quality bar: {score}/{target} ({status})."]
    if detail:
        lines.append(f"Quality bar verdict: {detail}")
    if next_action_label:
        action_line = f"Quality bar next action: {next_action_label}"
        if next_action_detail:
            action_line += f" - {next_action_detail}"
        lines.append(action_line)
    gaps = bar.get("gaps")
    if isinstance(gaps, list) and gaps:
        lines.append("Quality bar gaps:")
        for gap in gaps[:AGENT_OS_QUALITY_PROBLEM_PREVIEW_LIMIT]:
            if not isinstance(gap, Mapping):
                continue
            label = str(gap.get("label") or gap.get("key") or "Quality signal").strip()
            gap_score = int(gap.get("score") or 0)
            lines.append(f"- {label}: {gap_score}/{target}")
    return lines


def _autopilot_operator_inbox_lines(readiness: Mapping[str, Any]) -> list[str]:
    inbox = _mapping_payload(readiness.get("operator_inbox"))
    if not inbox:
        return []
    next_action_label = str(inbox.get("next_action_label") or "").strip()
    if not next_action_label:
        return []
    next_action_detail = str(inbox.get("next_action_detail") or "").strip()
    next_action_agent = str(inbox.get("next_action_agent") or "").strip()
    next_action_run_id = str(inbox.get("next_action_run_id") or "").strip()
    line = f"Operator inbox next action: {next_action_label}"
    if next_action_agent:
        line += f" - {next_action_agent}"
    if next_action_detail:
        line += f". {next_action_detail}"
    lines = [line]
    if next_action_run_id:
        lines.append(f"Operator inbox target run: {next_action_run_id}")
    return lines


def _autopilot_runtime_queue_lines(readiness: Mapping[str, Any]) -> list[str]:
    queue = _mapping_payload(readiness.get("runtime_queue"))
    if not queue:
        return []
    status = str(queue.get("status") or "checking").replace("_", " ")
    detail = str(queue.get("detail") or "").strip()
    fresh_active_raw = queue.get("fresh_active_count")
    fresh_active_count = (
        int(fresh_active_raw)
        if fresh_active_raw is not None
        else int(queue.get("active_count") or 0)
    )
    lines = [
        (
            "Runtime queue recovery: "
            f"{int(queue.get('queued_count') or 0)} queued, "
            f"{fresh_active_count} active, "
            f"{int(queue.get('waiting_count') or 0)} waiting, "
            f"{int(queue.get('stale_active_count') or 0)} stale ({status})."
        )
    ]
    if detail:
        lines.append(f"Runtime queue detail: {detail}")
    next_action_label = str(queue.get("next_action_label") or "").strip()
    next_action_detail = str(queue.get("next_action_detail") or "").strip()
    next_action_run_id = str(queue.get("next_action_run_id") or "").strip()
    if next_action_label:
        action_line = f"Runtime queue next action: {next_action_label}"
        if next_action_detail:
            action_line += f" - {next_action_detail}"
        lines.append(action_line)
    if next_action_run_id:
        lines.append(f"Runtime queue target run: {next_action_run_id}")
    problems = _string_items(
        queue.get("problems"),
        limit=AGENT_OS_QUALITY_PROBLEM_PREVIEW_LIMIT,
    )
    for problem in problems:
        lines.append(f"Runtime queue problem: {problem}")

    preview_lines: list[str] = []
    preview_run_ids: set[str] = set()
    for label, key in (
        ("stale", "stale_active_runs"),
        ("active", "fresh_active_runs"),
        ("queued", "queued_runs"),
        ("waiting", "waiting_runs"),
    ):
        runs = queue.get(key)
        if key == "fresh_active_runs" and not isinstance(runs, list):
            runs = queue.get("active_runs")
        if not isinstance(runs, list):
            continue
        for item in runs:
            if not isinstance(item, Mapping):
                continue
            run_id = str(item.get("run_id") or "").strip()
            if not run_id:
                continue
            if run_id in preview_run_ids:
                continue
            preview_run_ids.add(run_id)
            status_label = str(item.get("status") or "").strip().replace("_", " ")
            stage_label = str(item.get("stage") or "").strip().replace("_", " ")
            plan_label = str(item.get("plan_status") or "").strip().replace("_", " ")
            agent_profile_id = item.get("agent_profile_id")
            last_seen_age = item.get("last_seen_age_minutes")
            details = [
                part
                for part in (
                    f"profile {agent_profile_id}" if agent_profile_id else "",
                    status_label,
                    f"stage {stage_label}" if stage_label else "",
                    f"plan {plan_label}" if plan_label else "",
                    (
                        f"last seen {int(last_seen_age)}m ago"
                        if label == "stale" and last_seen_age is not None
                        else ""
                    ),
                )
                if part
            ]
            suffix = f" ({'; '.join(details)})" if details else ""
            preview_lines.append(f"Runtime {label} target: {run_id}{suffix}")
            if len(preview_lines) >= AGENT_OS_QUALITY_PROBLEM_PREVIEW_LIMIT:
                break
        if len(preview_lines) >= AGENT_OS_QUALITY_PROBLEM_PREVIEW_LIMIT:
            break
    if preview_lines:
        lines.append("Runtime queue targets:")
        lines.extend(preview_lines)
    return lines


def _autopilot_quality_monitor_lines(readiness: Mapping[str, Any]) -> list[str]:
    monitor = _mapping_payload(readiness.get(AGENT_QUALITY_MONITOR_KEY))
    if not monitor:
        return []
    status = str(monitor.get("status") or "checking").replace("_", " ")
    score = int(monitor.get("score") or 0)
    detail = str(monitor.get("detail") or "").strip()
    next_action_label = str(monitor.get("next_action_label") or "").strip()
    next_action_detail = str(monitor.get("next_action_detail") or "").strip()
    lines = [f"Local quality monitor: {status} ({score}/100)."]
    if detail:
        lines.append(f"Quality verdict: {detail}")
    if next_action_label:
        action_line = f"Quality next action: {next_action_label}"
        if next_action_detail:
            action_line += f" - {next_action_detail}"
        lines.append(action_line)

    dimensions = monitor.get("dimensions")
    problem_dimensions: list[str] = []
    if isinstance(dimensions, list):
        for dimension in dimensions:
            if not isinstance(dimension, Mapping):
                continue
            if dimension.get("status") == AGENT_OS_READINESS_CHECK_PASSED:
                continue
            label = str(dimension.get("label") or dimension.get("key") or "Quality signal").strip()
            dimension_detail = str(dimension.get("detail") or "needs attention").strip()
            problem_dimensions.append(f"{label}: {dimension_detail}")
            if len(problem_dimensions) >= AGENT_OS_QUALITY_PROBLEM_PREVIEW_LIMIT:
                break
    if problem_dimensions:
        lines.append("Quality watchlist:")
        lines.extend(f"- {item}" for item in problem_dimensions)
    return lines


def _autopilot_codex_bench_lines(readiness: Mapping[str, Any]) -> list[str]:
    bench = _mapping_payload(readiness.get(AGENT_CODEX_BENCH_KEY))
    if not bench:
        return []
    status = str(bench.get("status") or "checking").replace("_", " ")
    detail = str(bench.get("detail") or "").strip()
    next_action_label = str(bench.get("next_action_label") or "").strip()
    next_action_detail = str(bench.get("next_action_detail") or "").strip()
    lines = [
        (
            "Codex bench: "
            f"{status}; {int(bench.get('current_imported_count') or 0)}/"
            f"{int(bench.get('matching_count') or 0)} current automation(s) mirrored."
        )
    ]
    if detail:
        lines.append(f"Codex bench detail: {detail}")
    if next_action_label:
        action_line = f"Codex bench next action: {next_action_label}"
        if next_action_detail:
            action_line += f" - {next_action_detail}"
        lines.append(action_line)
    return lines


def _autopilot_capability_audit_lines(readiness: Mapping[str, Any]) -> list[str]:
    audit = _mapping_payload(readiness.get(AGENT_OS_CAPABILITY_AUDIT_KEY))
    if not audit:
        return []
    status = str(audit.get("status") or "checking").replace("_", " ")
    score = int(audit.get("score") or 0)
    detail = str(audit.get("detail") or "").strip()
    next_action_label = str(audit.get("next_action_label") or "").strip()
    next_action_detail = str(audit.get("next_action_detail") or "").strip()
    lines = [f"Agent OS capability audit: {status} ({score}/100)."]
    if detail:
        lines.append(f"Capability verdict: {detail}")
    if next_action_label:
        action_line = f"Capability next action: {next_action_label}"
        if next_action_detail:
            action_line += f" - {next_action_detail}"
        lines.append(action_line)
    gaps = audit.get("gaps")
    if isinstance(gaps, list) and gaps:
        lines.append("Capability gaps:")
        for gap in gaps[:AGENT_OS_QUALITY_PROBLEM_PREVIEW_LIMIT]:
            if not isinstance(gap, Mapping):
                continue
            label = str(gap.get("label") or gap.get("key") or "Capability").strip()
            gap_detail = str(gap.get("detail") or "needs attention").strip()
            lines.append(f"- {label}: {gap_detail}")
    return lines


def _autopilot_command_doctor(
    db: Session,
    row: ProjectAutonomyRun,
    *,
    user_id: int | None,
) -> str:
    if row.repo_id is None:
        return (
            f"{AUTOPILOT_DOCTOR_TITLE}: {AUTOPILOT_DOCTOR_NEEDS_ATTENTION_COPY}.\n"
            "Repo wiring: this run is not attached to a local repo, so I cannot audit agents, "
            "schedules, model policy, or safety gates yet."
        )
    try:
        readiness = agent_os_readiness(db, repo_id=int(row.repo_id), user_id=user_id)
    except ValueError as exc:
        return (
            f"{AUTOPILOT_DOCTOR_TITLE}: {AUTOPILOT_DOCTOR_NEEDS_ATTENTION_COPY}.\n"
            f"Repo wiring: {exc}"
        )

    status_label = _autopilot_doctor_status_label(readiness.get("status"))
    score = int(readiness.get("score") or 0)
    repo = readiness.get("repo") if isinstance(readiness.get("repo"), Mapping) else {}
    agents = readiness.get("agents") if isinstance(readiness.get("agents"), Mapping) else {}
    local_model = readiness.get("local_model") if isinstance(readiness.get("local_model"), Mapping) else {}
    operator_inbox = (
        readiness.get("operator_inbox")
        if isinstance(readiness.get("operator_inbox"), Mapping)
        else {}
    )
    codex_alignment = (
        readiness.get("codex_alignment")
        if isinstance(readiness.get("codex_alignment"), Mapping)
        else {}
    )
    codex = (
        readiness.get("codex_automations")
        if isinstance(readiness.get("codex_automations"), Mapping)
        else {}
    )
    codex_schedule = codex.get("schedule_mirror") if isinstance(codex.get("schedule_mirror"), Mapping) else {}
    model_name = str(local_model.get("model") or "none")
    model_detail = str(local_model.get("detail") or "No local model readiness detail was reported.")
    pending_questions = int(agents.get("pending_questions") or 0)
    active_schedules = int(agents.get("active_schedules") or 0)
    safety_detail = _readiness_check_detail(
        readiness,
        AGENT_OS_READINESS_CHECK_SAFE_DEFAULTS,
        "Mutation safety check did not report a detail.",
    )
    schedule_detail = _readiness_check_detail(
        readiness,
        AGENT_OS_READINESS_CHECK_SCHEDULES,
        "Schedule check did not report a detail.",
    )
    inbox_detail = _readiness_check_detail(
        readiness,
        AGENT_OS_READINESS_CHECK_OPERATOR_INBOX,
        "Operator inbox check did not report a detail.",
    )
    codex_alignment_detail = _readiness_check_detail(
        readiness,
        AGENT_OS_READINESS_CHECK_CODEX_ALIGNMENT,
        "Codex alignment check did not report a detail.",
    )
    question_detail = _readiness_check_detail(
        readiness,
        AGENT_OS_READINESS_CHECK_QUESTIONS,
        "Operator question check did not report a detail.",
    )

    lines = [
        f"{AUTOPILOT_DOCTOR_TITLE}: {status_label} ({score}/100).",
        f"Repo: {repo.get('name') or 'repo'} at {repo.get('resolved_path') or repo.get('path') or 'unknown path'}.",
        f"Local model: {model_name}. {model_detail}",
    ]
    quality_monitor_lines = _autopilot_quality_monitor_lines(readiness)
    codex_bench_lines = _autopilot_codex_bench_lines(readiness)
    capability_audit_lines = _autopilot_capability_audit_lines(readiness)
    coding_quality_bar_lines = _autopilot_coding_quality_bar_lines(readiness)
    runtime_queue_lines = _autopilot_runtime_queue_lines(readiness)
    operator_inbox_lines = _autopilot_operator_inbox_lines(readiness)
    if not bool(local_model.get("coding_ready")):
        lines.append(f"Model next step: {local_model.get('recommendation') or AGENT_OS_LOCAL_MODEL_RECOMMENDATION}")
    lines.extend(
        [
            (
                "Agents: "
                f"{int(agents.get('total') or 0)} total; "
                f"{int(agents.get('macro') or 0)} macro, "
                f"{int(agents.get('micro') or 0)} micro, "
                f"{int(agents.get('specialist') or 0)} specialist."
            ),
            (
                "Codex parity: "
                f"imported {int(codex.get('imported') or 0)} of "
                f"{int(codex.get('matching') or 0)} matching local automations."
            ),
            (
                "Codex schedules: "
                f"{int(codex_schedule.get('source_active_enabled') or 0)} of "
                f"{int(codex_schedule.get('source_active') or 0)} source-active automations enabled in CHILI."
            ),
            f"Schedules: {active_schedules} active. {schedule_detail}",
            f"Safety: {safety_detail}",
            f"Inbox: {int(operator_inbox.get('total_action_count') or 0)} action(s). {inbox_detail}",
            f"Codex alignment: {int(codex_alignment.get('score') or 0)}%. {codex_alignment_detail}",
            f"Questions: {pending_questions} pending. {question_detail}",
        ]
    )
    lines.extend(codex_bench_lines)
    lines.extend(coding_quality_bar_lines)
    lines.extend(runtime_queue_lines)
    lines.extend(operator_inbox_lines)
    lines.extend(quality_monitor_lines)
    lines.extend(capability_audit_lines)

    checks = readiness.get("checks")
    problem_checks = [
        check
        for check in checks
        if isinstance(check, Mapping)
        and check.get("status") in {AGENT_OS_READINESS_CHECK_WARNING, AGENT_OS_READINESS_CHECK_FAILED}
    ] if isinstance(checks, list) else []
    if not problem_checks:
        lines.append(AUTOPILOT_DOCTOR_NO_ACTION_COPY)
        return "\n".join(lines)

    lines.append("Needs attention:")
    for check in problem_checks:
        label = str(check.get("label") or check.get("key") or "Check").strip()
        detail = str(check.get("detail") or "No detail reported.").strip()
        lines.append(f"- {label}: {detail}")
    return "\n".join(lines)


def _autopilot_command_quality(
    db: Session,
    row: ProjectAutonomyRun,
    *,
    user_id: int | None,
) -> str:
    local_model = _local_model_readiness_payload(select_local_model())
    model_name = str(local_model.get("model") or "none")
    model_state = (
        "coder-ready"
        if bool(local_model.get("coding_ready"))
        else "general/local fallback"
    )
    quality_lines = [
        "Autopilot quality report:",
        f"- Local model: {model_name} ({model_state}). {local_model.get('detail')}",
        (
            "- Architect gate: plans must pass "
            f"{ARCHITECT_REVIEW_PASSING_SCORE}/100 before approval or implementation."
        ),
        (
            "- Scheduled-agent guard: plan-only cycle reports must score "
            f"{SCHEDULED_AGENT_REPORT_QUALITY_PASSING_SCORE}/100 and are repaired if they claim edits, tests, commits, or merges they did not perform."
        ),
        "- Safety posture: generated agents default to observe, research, and plan; worktree patches and merge are off unless explicitly enabled.",
        "- Execution posture: Plan Mode is approval-first; merge still requires clean validation and merge gates.",
    ]
    if not bool(local_model.get("coding_ready")):
        quality_lines.append(
            f"- Recommended upgrade path: {local_model.get('recommendation') or AGENT_OS_LOCAL_MODEL_RECOMMENDATION}"
        )
    if row.repo_id is not None:
        try:
            readiness = agent_os_readiness(db, repo_id=int(row.repo_id), user_id=user_id)
        except ValueError:
            readiness = {}
        if readiness:
            agents = _mapping_payload(readiness.get("agents"))
            codex = _mapping_payload(readiness.get("codex_automations"))
            schedule = _mapping_payload(codex.get("schedule_mirror"))
            scorecard = _mapping_payload(readiness.get("quality_scorecard"))
            reviews = _mapping_payload(scorecard.get("architect_reviews"))
            scheduled_quality = _mapping_payload(scorecard.get("scheduled_quality"))
            validation = _mapping_payload(scorecard.get("validation"))
            runtime_queue = _mapping_payload(readiness.get("runtime_queue"))
            operator_inbox = _mapping_payload(readiness.get("operator_inbox"))
            codex_alignment = _mapping_payload(readiness.get("codex_alignment"))
            codex_bench_lines = _autopilot_codex_bench_lines(readiness)
            capability_audit_lines = _autopilot_capability_audit_lines(readiness)
            problems = _string_items(scorecard.get("problems"), limit=AGENT_OS_QUALITY_PROBLEM_PREVIEW_LIMIT)
            quality_monitor_lines = _autopilot_quality_monitor_lines(readiness)
            coding_quality_bar_lines = _autopilot_coding_quality_bar_lines(readiness)
            runtime_queue_lines = _autopilot_runtime_queue_lines(readiness)
            operator_inbox_lines = _autopilot_operator_inbox_lines(readiness)
            quality_lines.extend(
                [
                    (
                        "- Agent bench: "
                        f"{int(agents.get('total') or 0)} profiles, "
                        f"{int(codex.get('current_imported') or codex.get('imported') or 0)}/"
                        f"{int(codex.get('matching') or 0)} current Codex automations imported; "
                        f"{int(codex.get('historical_imported') or 0)} historical profile(s) retained."
                    ),
                    (
                        "- Codex schedule mirror: "
                        f"{int(schedule.get('source_active_enabled') or 0)} of "
                        f"{int(schedule.get('source_active') or 0)} source-active Codex automations enabled in CHILI."
                    ),
                    (
                        "- Quality scorecard: "
                        f"{int(scorecard.get('recent_run_count') or 0)} recent run(s), "
                        f"{int(reviews.get('passed') or 0)} passing plan review(s), "
                        f"{int(reviews.get('blocked') or 0)} blocked weak plan(s), "
                        f"{int(scheduled_quality.get('repaired') or 0)} repaired schedule report(s), "
                        f"{int(validation.get('passed') or 0)}/{int(validation.get('total') or 0)} validation set(s) passed."
                    ),
                    (
                        "- Runtime queue: "
                        f"{int(runtime_queue.get('queued_count') or 0)} queued, "
                        f"{int(runtime_queue.get('active_count') or 0)} active, "
                        f"{int(runtime_queue.get('waiting_count') or 0)} waiting, "
                        f"{int(runtime_queue.get('always_on_profile_count') or 0)} always-on profile(s)."
                    ),
                    (
                        "- Operator inbox: "
                        f"{int(operator_inbox.get('total_action_count') or 0)} action(s), "
                        f"{int(operator_inbox.get('approval_count') or 0)} approval(s), "
                        f"{int(operator_inbox.get('clarification_count') or 0)} clarification(s), "
                        f"{int(operator_inbox.get('pending_question_count') or 0)} question(s)."
                    ),
                    (
                        "- Local-vs-Codex alignment: "
                        f"{int(codex_alignment.get('score') or 0)}% across "
                        f"{int(codex_alignment.get('dimension_count') or 0)} dimension(s); "
                        f"{codex_alignment.get('detail') or 'no Codex comparison detail reported.'}"
                    ),
                ]
            )
            quality_lines.extend(f"- {line}" for line in codex_bench_lines)
            quality_lines.extend(f"- {line}" for line in coding_quality_bar_lines)
            quality_lines.extend(f"- {line}" for line in runtime_queue_lines)
            quality_lines.extend(f"- {line}" for line in operator_inbox_lines)
            quality_lines.extend(f"- {line}" for line in quality_monitor_lines)
            quality_lines.extend(f"- {line}" for line in capability_audit_lines)
            if problems:
                quality_lines.append("- Quality attention: " + " ".join(problems))
    quality_lines.append(
        "Bottom line: local model quality is not trusted blindly; CHILI presents weak plans as clarification or revision instead of approval-ready work."
    )
    return "\n".join(str(line) for line in quality_lines if str(line).strip())


def _clean_reference_path_arg(args: str) -> Path | None:
    raw = (args or "").strip().strip('"').strip("'")
    if not raw:
        return None
    return Path(raw).expanduser()


def _reference_intake_candidate_files(root: Path) -> list[Path]:
    candidates: list[Path] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop(0)
        try:
            children = list(current.iterdir())
        except OSError:
            continue
        for child in children:
            name = child.name
            if child.is_file() and name in REFERENCE_INTAKE_MANIFEST_NAMES | REFERENCE_INTAKE_TEXT_NAMES:
                candidates.append(child)
                continue
            if child.is_dir() and depth < REFERENCE_INTAKE_MAX_DEPTH and name not in {
                ".git",
                "node_modules",
                "src",
                "dist",
                "build",
                ".next",
            }:
                stack.append((child, depth + 1))
    return candidates


def _read_reference_text(path: Path) -> str:
    try:
        if path.stat().st_size > REFERENCE_INTAKE_MAX_FILE_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _reference_manifest_payload(path: Path) -> dict[str, Any]:
    text = _read_reference_text(path)
    if not text:
        return {}
    if path.name == "package.json":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}
    if path.name == "pyproject.toml":
        try:
            payload = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return {}
        project = payload.get("project") if isinstance(payload, dict) else {}
        tool = payload.get("tool") if isinstance(payload, dict) else {}
        poetry = tool.get("poetry") if isinstance(tool, dict) else {}
        return project if isinstance(project, dict) and project else poetry if isinstance(poetry, dict) else {}
    return {}


def _reference_dependency_names(payload: Mapping[str, Any]) -> list[str]:
    names: list[str] = []
    for key in ("dependencies", "devDependencies", "optionalDependencies"):
        raw = payload.get(key)
        if isinstance(raw, Mapping):
            names.extend(str(name) for name in raw.keys())
    return sorted(set(names), key=str.lower)


def _reference_clean_room_signals(dependencies: Iterable[str]) -> list[str]:
    signals: list[str] = []
    for dependency in dependencies:
        clean = str(dependency).strip()
        if clean in REFERENCE_INTAKE_SAFE_PACKAGE_SIGNALS:
            signals.append(REFERENCE_INTAKE_SAFE_PACKAGE_SIGNALS[clean])
            continue
        for marker, signal in REFERENCE_INTAKE_SAFE_PACKAGE_SIGNALS.items():
            if marker in clean:
                signals.append(signal)
                break
    unique: list[str] = []
    for signal in signals:
        if signal not in unique:
            unique.append(signal)
    return unique


def _reference_intake_report(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "status": REFERENCE_INTAKE_STATUS_CAUTION,
            "path": str(path),
            "reasons": ["Path does not exist on this machine."],
            "signals": [],
        }
    if not path.is_dir():
        return {
            "status": REFERENCE_INTAKE_STATUS_CAUTION,
            "path": str(path),
            "reasons": ["Reference intake expects a local folder."],
            "signals": [],
        }
    candidates = _reference_intake_candidate_files(path)
    manifests = [candidate for candidate in candidates if candidate.name in REFERENCE_INTAKE_MANIFEST_NAMES]
    text_files = [candidate for candidate in candidates if candidate.name in REFERENCE_INTAKE_TEXT_NAMES]
    reasons: list[str] = []
    dependencies: list[str] = []
    package_summaries: list[str] = []
    licenses: list[str] = []

    for manifest in manifests:
        payload = _reference_manifest_payload(manifest)
        if not payload:
            continue
        name = str(payload.get("name") or manifest.parent.name).strip()
        version = str(payload.get("version") or "").strip()
        description = str(payload.get("description") or "").strip()
        license_name = str(payload.get("license") or "").strip()
        if license_name:
            licenses.append(license_name)
        summary = name
        if version:
            summary += f"@{version}"
        if license_name:
            summary += f" ({license_name})"
        package_summaries.append(summary)
        searchable = " ".join([name, version, description, license_name]).lower()
        for marker in REFERENCE_INTAKE_TAINT_MARKERS:
            if marker in searchable:
                reasons.append(f"{manifest.name} advertises {marker}.")
        dependencies.extend(_reference_dependency_names(payload))

    if not reasons:
        for text_file in text_files:
            text = _read_reference_text(text_file).lower()
            if not text:
                continue
            for marker in REFERENCE_INTAKE_TAINT_MARKERS:
                if marker in text:
                    reasons.append(f"{text_file.name} mentions {marker}.")

    license_set = {license_name.lower() for license_name in licenses}
    if license_set & REFERENCE_INTAKE_CAUTION_LICENSES:
        reasons.append("One or more manifests are unlicensed or proprietary.")
    if not manifests:
        reasons.append("No supported manifest was found, so provenance is unknown.")

    status = REFERENCE_INTAKE_STATUS_TAINTED if reasons else REFERENCE_INTAKE_STATUS_SAFE
    if status == REFERENCE_INTAKE_STATUS_SAFE and not licenses:
        status = REFERENCE_INTAKE_STATUS_CAUTION
        reasons.append("No explicit license was found in scanned manifests.")

    return {
        "status": status,
        "path": str(path),
        "reasons": sorted(set(reasons)),
        "packages": package_summaries[:REFERENCE_INTAKE_PREVIEW_LIMIT],
        "signals": _reference_clean_room_signals(dependencies)[:REFERENCE_INTAKE_PREVIEW_LIMIT],
    }


def _autopilot_command_reference(args: str) -> str:
    path = _clean_reference_path_arg(args)
    if path is None:
        return "Usage: /reference C:\\path\\to\\candidate-folder"
    report = _reference_intake_report(path)
    status = str(report.get("status") or REFERENCE_INTAKE_STATUS_CAUTION)
    title = {
        REFERENCE_INTAKE_STATUS_SAFE: "Reference intake: clean-room usable.",
        REFERENCE_INTAKE_STATUS_CAUTION: "Reference intake: caution.",
        REFERENCE_INTAKE_STATUS_TAINTED: "Reference intake: tainted source blocked.",
    }.get(status, "Reference intake: caution.")
    lines = [title, f"Path: {report.get('path')}"]
    packages = _string_items(report.get("packages"), limit=REFERENCE_INTAKE_PREVIEW_LIMIT)
    if packages:
        lines.append("Scanned manifests: " + ", ".join(packages) + ".")
    reasons = _string_items(report.get("reasons"), limit=REFERENCE_INTAKE_PREVIEW_LIMIT)
    if reasons:
        lines.append("Why: " + " ".join(reasons))
    signals = _string_items(report.get("signals"), limit=REFERENCE_INTAKE_PREVIEW_LIMIT)
    if signals:
        lines.append("Clean-room salvage ideas from public metadata:")
        lines.extend(f"- {signal}." for signal in signals)
    if status == REFERENCE_INTAKE_STATUS_TAINTED:
        lines.append(
            "I will not read, copy, summarize, or train from its source/prompts/docs. "
            "I can implement comparable CHILI features from official docs, public package APIs, and our own design."
        )
    else:
        lines.append(
            "Before any reuse, keep changes clean-room: cite license, prefer public APIs, and implement in CHILI's patterns."
        )
    return "\n".join(lines)


def _autopilot_command_questions(
    db: Session,
    row: ProjectAutonomyRun,
) -> str:
    questions = (
        db.query(ProjectAutonomyOperatorQuestion)
        .filter(
            ProjectAutonomyOperatorQuestion.run_id == row.run_id,
            ProjectAutonomyOperatorQuestion.status == OPERATOR_QUESTION_STATUS_PENDING,
        )
        .order_by(ProjectAutonomyOperatorQuestion.created_at.asc(), ProjectAutonomyOperatorQuestion.id.asc())
        .all()
    )
    if not questions:
        return "No pending operator questions for this run."
    lines = ["Pending operator questions:"]
    for index, question in enumerate(questions, start=1):
        lines.append(f"{index}. {question.question}")
    return "\n".join(lines)


def _autopilot_command_schedule(
    db: Session,
    row: ProjectAutonomyRun,
    args: str,
) -> str:
    profile = _agent_profile_for_run(db, row)
    if profile is None:
        return "This run is not bound to an agent profile, so it has no schedule."
    action = (args or "").strip().lower()
    if action in {
        AUTOPILOT_COMMAND_SCHEDULE_CODEX,
        AUTOPILOT_COMMAND_SCHEDULE_CODEX_ACTIVE,
        AUTOPILOT_COMMAND_SCHEDULE_CODEX_ALWAYS_ON,
        AUTOPILOT_COMMAND_SCHEDULE_CODEX_PAUSE,
    }:
        return _autopilot_command_codex_schedule(db, row, user_id=row.user_id, action=action)
    if action in {AUTOPILOT_COMMAND_SCHEDULE_ON, AUTOPILOT_COMMAND_SCHEDULE_RESUME}:
        schedule_config = _json_load(profile.schedule_json, {})
        if not isinstance(schedule_config, dict):
            schedule_config = _default_agent_schedule_config()
        if not schedule_config.get("rrule"):
            schedule_config["rrule"] = AUTOPILOT_COMMAND_SCHEDULE_DEFAULT_RRULE
            schedule_config["cadence"] = CODEX_AUTOMATION_CADENCE_TEN_MINUTES
        profile.status = AGENT_PROFILE_STATUS_ACTIVE
        profile.schedule_enabled = True
        profile.schedule_json = _json_text(schedule_config)
        _sync_agent_schedule_row(db, profile)
        payload = _agent_profile_payload(db, profile)
        return f"Schedule enabled for {profile.name}: {_format_profile_schedule(payload)}."
    if action in {AUTOPILOT_COMMAND_SCHEDULE_OFF, AUTOPILOT_COMMAND_SCHEDULE_PAUSE}:
        profile.schedule_enabled = False
        if action == AUTOPILOT_COMMAND_SCHEDULE_PAUSE:
            profile.status = AGENT_PROFILE_STATUS_PAUSED
        _sync_agent_schedule_row(db, profile)
        payload = _agent_profile_payload(db, profile)
        return f"Schedule paused for {profile.name}: {_format_profile_schedule(payload)}."
    payload = _agent_profile_payload(db, profile)
    return f"{profile.name} schedule is {_format_profile_schedule(payload)}."


def _handle_autopilot_slash_command(
    db: Session,
    row: ProjectAutonomyRun,
    *,
    command: str,
    args: str,
    user_id: int | None,
) -> bool:
    if command not in AUTOPILOT_SLASH_COMMANDS:
        _record_message(
            db,
            row,
            "assistant",
            f"I don't know /{command}. Try /help for the available Autopilot commands.",
            message_type=AUTOPILOT_COMMAND_MESSAGE_TYPE,
            commit=False,
        )
        return True
    if command == AUTOPILOT_COMMAND_HELP:
        reply = _autopilot_command_help()
    elif command == AUTOPILOT_COMMAND_STATUS:
        reply = _autopilot_command_status(db, row)
    elif command == AUTOPILOT_COMMAND_AGENTS:
        reply = _autopilot_command_agents(db, row, user_id=user_id)
    elif command == AUTOPILOT_COMMAND_MODEL:
        reply = _autopilot_command_model(db, row)
    elif command == AUTOPILOT_COMMAND_DOCTOR:
        reply = _autopilot_command_doctor(db, row, user_id=user_id)
    elif command == AUTOPILOT_COMMAND_QUALITY:
        reply = _autopilot_command_quality(db, row, user_id=user_id)
    elif command == AUTOPILOT_COMMAND_REFERENCE:
        reply = _autopilot_command_reference(args)
    elif command == AUTOPILOT_COMMAND_QUESTIONS:
        reply = _autopilot_command_questions(db, row)
    elif command == AUTOPILOT_COMMAND_SCHEDULE:
        reply = _autopilot_command_schedule(db, row, args)
    elif command == AUTOPILOT_COMMAND_PLAN:
        _mark_plan_requested(db, row)
        return True
    elif command == AUTOPILOT_COMMAND_APPROVE:
        try:
            _approve_plan_row(db, row, action="slash approval")
        except ValueError as exc:
            _record_message(
                db,
                row,
                "assistant",
                f"Approve unavailable: {str(exc)}",
                message_type=AUTOPILOT_COMMAND_MESSAGE_TYPE,
                commit=False,
            )
        return True
    elif command == AUTOPILOT_COMMAND_CANCEL:
        _cancel_run_row(db, row)
        return True
    elif command == AUTOPILOT_COMMAND_CLEAR:
        row.archived_at = _utcnow()
        row.archive_reason = AUTOPILOT_COMMAND_CLEAR_ARCHIVE_REASON
        reply = "Archived this Autopilot chat. Audit data, messages, artifacts, and learning samples were preserved."
    else:
        reply = _autopilot_command_help()
    _record_message(
        db,
        row,
        "assistant",
        reply,
        message_type=AUTOPILOT_COMMAND_MESSAGE_TYPE,
        commit=False,
    )
    return True


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
    slash_command = None if clean_attachments else _parse_autopilot_slash_command(clean)
    if slash_command is not None:
        command, args = slash_command
        _handle_autopilot_slash_command(
            db,
            row,
            command=command,
            args=args,
            user_id=user_id,
        )
        db.commit()
        db.refresh(row)
        return run_payload(db, row, include_events=True)
    if not clean_attachments and _looks_like_run_cancel_message(clean):
        _cancel_run_row(db, row)
        db.commit()
        db.refresh(row)
        return run_payload(db, row, include_events=True)
    if row.status == RUN_STATUS_AWAITING_CLARIFICATION or row.plan_status == PLAN_STATUS_AWAITING_CLARIFICATION:
        _answer_pending_operator_questions(db, row, display_content, commit=False)
    if row.status == RUN_STATUS_CHATTING:
        if _looks_like_plan_start_prompt(display_content):
            _mark_plan_requested(db, row)
        else:
            reply = _chat_reply(db, row, display_content)
            _record_message(db, row, "assistant", reply, message_type="chat", commit=False)
    if (
        row.status == RUN_STATUS_AWAITING_APPROVAL
        and row.plan_status == PLAN_STATUS_AWAITING_APPROVAL
        and not clean_attachments
        and _looks_like_plan_approval_message(clean)
    ):
        try:
            _approve_plan_row(db, row, action="chat approval")
        except ValueError as exc:
            _record_message(
                db,
                row,
                "assistant",
                f"I can't approve this plan yet. {str(exc)}",
                message_type="status",
                commit=False,
            )
            db.commit()
            db.refresh(row)
            return run_payload(db, row, include_events=True)
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
        row.current_stage = STAGE_PLAN
        row.prompt = _conversation_prompt(db, row)
        _clear_plan_execution_state(row)
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


def _approve_plan_row(db: Session, row: ProjectAutonomyRun, *, action: str) -> None:
    plan = _json_load(row.plan_json, {})
    if not plan:
        raise ValueError("This run does not have a plan to approve yet.")
    review_blocker = _approved_plan_review_blocker(db, row, plan, action=action)
    if review_blocker:
        raise ValueError(review_blocker)
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


def approve_plan(db: Session, run_id: str, *, user_id: int | None = None) -> dict[str, Any] | None:
    row = _get_run_row(db, run_id, user_id=user_id)
    if row is None:
        return None
    if row.status != RUN_STATUS_AWAITING_APPROVAL or row.plan_status != PLAN_STATUS_AWAITING_APPROVAL:
        raise ValueError("This run is not waiting on an approval-ready architect plan.")
    _approve_plan_row(db, row, action="approval")
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
    _clear_plan_execution_state(row)
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
        "Got it. I'll scan the repo and draft a plan, then wait for your approval before editing files.",
        message_type="status",
        commit=False,
    )


def _clear_plan_execution_state(row: ProjectAutonomyRun) -> None:
    row.plan_json = "{}"
    row.files_json = "[]"
    row.agents_json = "[]"
    row.commands_json = "[]"
    row.validation_json = "[]"
    row.learning_json = "{}"
    row.target_branch = None
    row.base_branch = None
    row.base_sha = None
    row.integration_branch = None
    row.worktree_path = None
    row.merge_status = MERGE_STATUS_PENDING
    row.error_message = None
    row.merge_message = None
    row.cancel_requested = False
    row.started_at = None
    row.finished_at = None


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


def _agent_profile_by_key(
    db: Session,
    *,
    repo_id: int,
    user_id: int | None,
    profile_key: str,
) -> ProjectAutonomyAgentProfile | None:
    return (
        _agent_profile_query(db, user_id=user_id, repo_id=repo_id)
        .filter(ProjectAutonomyAgentProfile.profile_key == profile_key)
        .first()
    )


def _expert_haystack(prompt: str, files: list[dict[str, Any]]) -> str:
    paths = " ".join(str(item.get("path") or "") for item in files if isinstance(item, dict))
    descriptions = " ".join(str(item.get("description") or "") for item in files if isinstance(item, dict))
    return f"{prompt or ''} {paths} {descriptions}".lower()


def _has_any(haystack: str, tokens: Iterable[str]) -> bool:
    return any(token in haystack for token in tokens)


def _route_expert_profile_keys(prompt: str, files: list[dict[str, Any]]) -> list[str]:
    haystack = _expert_haystack(prompt, files)
    paths = [str(item.get("path") or "").lower() for item in files if isinstance(item, dict)]
    keys: list[str] = ["architect"]

    def add(*items: str) -> None:
        for item in items:
            if item not in keys:
                keys.append(item)

    has_code_file = any(
        path.endswith((".py", ".js", ".jsx", ".ts", ".tsx", ".dart", ".html", ".css", ".sql"))
        for path in paths
    )
    if has_code_file or _has_any(haystack, ("implement", "code", "change", "fix", "build", "refactor", "wire")):
        add("software_engineer")
    if any(path.endswith((".html", ".css", ".js", ".jsx", ".ts", ".tsx", ".dart")) for path in paths) or _has_any(
        haystack,
        ("frontend", "ui", "screen", "component", "browser", "css", "template", "flutter"),
    ):
        add("frontend")
    if any(path.endswith((".py", ".sql")) or path.startswith(("app/", "scripts/")) for path in paths) or _has_any(
        haystack,
        ("api", "backend", "service", "router", "endpoint", "worker", "orchestrator"),
    ):
        add("backend")
    if any(path.startswith("tests/") or "/tests/" in path or Path(path).name.startswith("test_") for path in paths):
        add("qa")
    if _has_any(
        haystack,
        ("schema", "migration", "database", "sqlite", "postgres", "query", "persistence", "model", "table"),
    ) or any(
        path.startswith(("app/models/", "migrations/", "alembic/")) or "migration" in path or path.endswith(".sql")
        for path in paths
    ):
        add("dba_architect", "db_quality", "qa")
    if _has_any(
        haystack,
        ("auth", "token", "secret", "credential", "permission", "security", "sandbox", "path traversal"),
    ):
        add("security")
    if _has_any(
        haystack,
        ("deploy", "docker", "ci", "github action", "workflow", "runtime", "observability", "logging", "scheduler"),
    ) or any(path.startswith((".github/", "docker", "scripts/")) or "docker" in path for path in paths):
        add("sre")
    if _has_any(haystack, ("model promotion", "fine tune", "eval", "mlops", "training", "reproducibility")):
        add("mlops")
    if _has_any(haystack, ("metric", "data quality", "dataset", "queue", "evaluation", "backtest", "sample")):
        add("data_scientist")
    if _has_any(
        haystack,
        (
            "trade",
            "trading",
            "broker",
            "portfolio",
            "capital",
            "position",
            "autotrader",
            "alpha",
            "options",
            "robinhood",
            "live-trading",
            "live trading",
        ),
    ) or any("/trading/" in path or path.startswith("app/services/trading") for path in paths):
        add("algo_trading_architect", "data_scientist", "risk_reviewer", "qa")
    if any(path.startswith("docs/") or path.endswith((".md", ".rst")) for path in paths) or _has_any(
        haystack,
        ("docs", "documentation", "readme", "strategy note"),
    ):
        add("docs")
    if len(keys) == 1:
        add("software_engineer", "qa")
    elif "qa" not in keys and (has_code_file or len(keys) > 2):
        add("qa")
    return keys


def _thread_files_for_profile(profile_key: str, files: list[dict[str, Any]]) -> list[str]:
    paths = [
        str(item.get("path") or "")
        for item in files
        if isinstance(item, dict) and str(item.get("path") or "").strip()
    ]
    if profile_key in {"architect", "software_engineer", "qa", "risk_reviewer"}:
        return paths[:12]
    if profile_key == "frontend":
        selected = [
            path
            for path in paths
            if path.lower().endswith((".html", ".css", ".js", ".jsx", ".ts", ".tsx", ".dart"))
            or path.lower().startswith(("app/static/", "app/templates/", "chili_mobile/"))
        ]
        return (selected or paths)[:12]
    if profile_key == "backend":
        selected = [
            path
            for path in paths
            if path.lower().endswith((".py", ".sql")) or path.lower().startswith(("app/", "scripts/"))
        ]
        return (selected or paths)[:12]
    if profile_key in {"dba_architect", "db_quality"}:
        selected = [
            path
            for path in paths
            if path.lower().startswith(("app/models/", "migrations/", "alembic/"))
            or "migration" in path.lower()
            or path.lower().endswith(".sql")
        ]
        return (selected or paths)[:12]
    if profile_key == "security":
        selected = [
            path
            for path in paths
            if _has_any(path.lower(), ("auth", "token", "secret", "credential", "permission", "security"))
        ]
        return (selected or paths)[:12]
    if profile_key in {"sre", "mlops"}:
        selected = [
            path
            for path in paths
            if path.lower().startswith((".github/", "scripts/")) or _has_any(path.lower(), ("docker", "workflow"))
        ]
        return (selected or paths)[:12]
    if profile_key in {"algo_trading_architect", "data_scientist"}:
        selected = [
            path
            for path in paths
            if "/trading/" in path.lower()
            or path.lower().startswith(("app/services/trading", "scripts/", "tests/test_auto", "tests/test_trading"))
        ]
        return (selected or paths)[:12]
    if profile_key == "docs":
        selected = [path for path in paths if path.lower().startswith("docs/") or path.lower().endswith((".md", ".rst"))]
        return (selected or paths)[:12]
    return paths[:12]


def _thread_expected_deliverable(profile_key: str) -> str:
    deliverables = {
        "architect": "Architecture review, implementation boundaries, and approval-readiness risks.",
        "software_engineer": "Implementation handoff with likely code seams, constraints, and test hooks.",
        "frontend": "UI behavior, state, and rendering handoff with visual QA notes.",
        "backend": "API/service/data-flow handoff with integration and regression risks.",
        "qa": "Focused validation plan, acceptance scenarios, and residual risk notes.",
        "dba_architect": "Schema, migration, persistence, and query-safety review.",
        "db_quality": "Data integrity, fixture, compatibility, and rollback-risk review.",
        "security": "Security, permissions, secrets, path, and unsafe-operation review.",
        "sre": "Runtime, deployment, observability, rollback, and operational safety review.",
        "mlops": "Model evaluation, promotion, reproducibility, and monitoring review.",
        "data_scientist": "Data-quality, metric, queue, and evidence review.",
        "risk_reviewer": "Safety-gate review for live-trading, broker, capital, destructive, or high-blast-radius risks.",
        "algo_trading_architect": "Trading-system architecture review without changing execution, broker, or capital behavior.",
        "docs": "Operator-facing documentation and acceptance-criteria handoff.",
    }
    return deliverables.get(profile_key, "Specialist review and handoff brief.")


def _thread_safety_constraints(profile_key: str) -> list[str]:
    constraints = [
        "Project domain only; do not change trading autopilot execution behavior in this workflow.",
        "Do not mutate files, run migrations, merge branches, or alter production state from an expert thread.",
        "Keep implementation approval-first through the existing Project Autopilot worktree gates.",
    ]
    if profile_key in {"algo_trading_architect", "data_scientist", "risk_reviewer"}:
        constraints.append(
            "Trading-sensitive work is review-only here: no broker execution, live-trading toggles, capital allocation, position identity, kill switches, or drawdown breaker changes without explicit authorization."
        )
    if profile_key in {"dba_architect", "db_quality"}:
        constraints.append(
            "Production schema changes, migrations, destructive database state, and compatibility views require explicit named safety checks."
        )
    if profile_key in {"sre", "mlops", "security"}:
        constraints.append(
            "Secrets, deploy state, credentials, infrastructure, and runtime policy changes require explicit operator approval."
        )
    return constraints


def _thread_safety_gate_summary(expert_threads: list[dict[str, Any]]) -> list[str]:
    gates: list[str] = []
    for thread in expert_threads:
        for constraint in thread.get("safety_constraints") or []:
            if constraint not in gates:
                gates.append(str(constraint))
            if len(gates) >= 6:
                return gates
    return gates


def _thread_dependencies(profile_key: str, keys: list[str]) -> list[str]:
    dependencies: list[str] = []
    if profile_key != "architect" and "architect" in keys:
        dependencies.append("architect")
    if profile_key not in {"qa", "risk_reviewer"} and "qa" in keys:
        dependencies.append("qa")
    if profile_key == "algo_trading_architect":
        for item in ("data_scientist", "risk_reviewer"):
            if item in keys:
                dependencies.append(item)
    if profile_key == "backend" and "dba_architect" in keys:
        dependencies.append("dba_architect")
    return dependencies


def _thread_deliverable_summary(profile: ProjectAutonomyAgentProfile, files: list[str]) -> str:
    scope = ", ".join(files[:4]) if files else "the operator prompt and architect plan"
    suffix = f", plus {len(files) - 4} more file(s)" if len(files) > 4 else ""
    return f"{profile.name} handoff is ready for {scope}{suffix}."


def _build_expert_thread(
    *,
    run: ProjectAutonomyRun,
    profile: ProjectAutonomyAgentProfile,
    profile_key: str,
    all_keys: list[str],
    plan: dict[str, Any],
    files: list[dict[str, Any]],
    review: dict[str, Any] | None,
) -> dict[str, Any]:
    thread_files = _thread_files_for_profile(profile_key, files)
    expected_deliverable = _thread_expected_deliverable(profile_key)
    request_brief = (
        f"PM request for {profile.name}: review the Project Autopilot plan for "
        f"{(run.prompt or '').strip()[:180] or 'this project request'}."
    )
    return {
        "thread_kind": "expert",
        "workflow_mode": EXPERT_WORKFLOW_MODE_PM_LED,
        "profile_key": profile.profile_key,
        "name": profile.profile_key,
        "display_name": profile.name,
        "role": profile.role,
        "tier": profile.tier,
        "status": "handoff_ready",
        "reply_to": run.run_id,
        "request_brief": request_brief,
        "expected_deliverable": expected_deliverable,
        "success_criteria": [
            "Identify risks, dependencies, and acceptance criteria for this specialist area.",
            "Stay review-only unless the parent Project Autopilot run is explicitly approved for implementation.",
        ],
        "dependencies": _thread_dependencies(profile_key, all_keys),
        "safety_constraints": _thread_safety_constraints(profile_key),
        "deliverable_summary": _thread_deliverable_summary(profile, thread_files),
        "files": thread_files,
        "plan_status": str((review or {}).get("status") or "drafted"),
        "plan_analysis": _operator_safe_plan_text((plan or {}).get("analysis")),
    }


def _expert_child_prompt(thread: dict[str, Any]) -> str:
    sections = [
        str(thread.get("request_brief") or "Review the parent Project Autopilot run."),
        "Expected Deliverable: " + str(thread.get("expected_deliverable") or "Specialist handoff brief."),
    ]
    files = thread.get("files") or []
    if files:
        sections.append("Files: " + ", ".join(str(item) for item in files[:12]))
    constraints = thread.get("safety_constraints") or []
    if constraints:
        sections.append("Safety Constraints: " + " | ".join(str(item) for item in constraints[:4]))
    return "\n\n".join(sections)


def _upsert_expert_child_run(
    db: Session,
    *,
    parent: ProjectAutonomyRun,
    child_profile: ProjectAutonomyAgentProfile,
    thread: dict[str, Any],
) -> str:
    delegation = (
        db.query(ProjectAutonomyDelegation)
        .filter(
            ProjectAutonomyDelegation.parent_run_id == parent.run_id,
            ProjectAutonomyDelegation.child_agent_profile_id == child_profile.id,
        )
        .order_by(ProjectAutonomyDelegation.id.desc())
        .first()
    )
    child = None
    if delegation is not None:
        child = (
            db.query(ProjectAutonomyRun)
            .filter(ProjectAutonomyRun.run_id == delegation.child_run_id)
            .first()
        )
    now = _utcnow()
    prompt = _expert_child_prompt(thread)
    plan_payload = {
        "analysis": thread.get("deliverable_summary") or "",
        "files": [
            {"path": path, "action": "review", "description": thread.get("expected_deliverable") or ""}
            for path in thread.get("files") or []
        ],
        "notes": "Expert thread is review-only; parent approval gates implementation.",
    }
    if child is None:
        child = ProjectAutonomyRun(
            run_id="pa_" + uuid.uuid4().hex[:14],
            user_id=parent.user_id,
            repo_id=parent.repo_id,
            agent_profile_id=child_profile.id,
            parent_run_id=parent.run_id,
            agent_snapshot_json=_json_text(_agent_profile_snapshot(child_profile)),
            prompt=prompt,
            status=RUN_STATUS_COMPLETED,
            current_stage=STAGE_COORDINATE,
            autonomy_level=EXPERT_WORKFLOW_CHILD_AUTONOMY_LEVEL,
            execution_mode=EXECUTION_MODE_PLAN_APPROVAL,
            plan_status="handoff_complete",
            chat_title=str(thread.get("display_name") or child_profile.name)[:120],
            model_policy=child_profile.model_policy or "local_first",
            merge_status=MERGE_STATUS_PENDING,
            started_at=now,
            finished_at=now,
        )
        db.add(child)
        db.flush()
        _record_step(
            db,
            child,
            STAGE_COORDINATE,
            f"{child_profile.name} expert handoff created",
            status="completed",
            agent_name=child_profile.profile_key,
            detail={"parent_run_id": parent.run_id, "profile_key": child_profile.profile_key},
            commit=False,
        )
        _record_message(
            db,
            child,
            "user",
            prompt,
            message_type="delegation_request",
            metadata={"parent_run_id": parent.run_id, "reply_to": parent.run_id},
            commit=False,
        )
    else:
        child.prompt = prompt
        child.agent_profile_id = child_profile.id
        child.agent_snapshot_json = _json_text(_agent_profile_snapshot(child_profile))
        child.status = RUN_STATUS_COMPLETED
        child.current_stage = STAGE_COORDINATE
        child.plan_status = "handoff_complete"
        child.finished_at = now
        child.updated_at = now
    child.plan_json = _json_text(plan_payload)
    child.files_json = _json_text(thread.get("files") or [])
    _record_message(
        db,
        child,
        "assistant",
        str(thread.get("deliverable_summary") or "Expert handoff ready."),
        message_type="expert_handoff",
        metadata={"thread": thread, "parent_run_id": parent.run_id},
        commit=False,
    )
    _add_artifact(
        db,
        child.run_id,
        "expert_thread",
        child_profile.profile_key,
        content_json=thread,
        commit=False,
    )
    if delegation is None:
        delegation = ProjectAutonomyDelegation(
            parent_run_id=parent.run_id,
            child_run_id=child.run_id,
            parent_agent_profile_id=parent.agent_profile_id,
            child_agent_profile_id=child_profile.id,
            status=DELEGATION_STATUS_COMPLETED,
            intent=_json_text(thread),
        )
        db.add(delegation)
    else:
        delegation.child_run_id = child.run_id
        delegation.parent_agent_profile_id = parent.agent_profile_id
        delegation.child_agent_profile_id = child_profile.id
        delegation.status = DELEGATION_STATUS_COMPLETED
        delegation.intent = _json_text(thread)
    db.flush()
    return child.run_id


def _pm_synthesis_for_threads(
    *,
    run: ProjectAutonomyRun,
    coordinator: ProjectAutonomyAgentProfile | None,
    threads: list[dict[str, Any]],
    review: dict[str, Any] | None,
) -> dict[str, Any]:
    safety_gates = _thread_safety_gate_summary(threads)
    blockers = [
        str(thread.get("display_name") or thread.get("profile_key"))
        for thread in threads
        if str(thread.get("status") or "").lower() in {"blocked", "failed"}
    ]
    decisions = [
        "PM is the parent coordinator for this Project Autopilot run.",
        "Specialist threads are review and handoff records, not independent file mutation.",
        "Implementation stays behind plan approval, worktree permission, validation, and merge gates.",
    ]
    if any(thread.get("profile_key") in {"algo_trading_architect", "risk_reviewer"} for thread in threads):
        decisions.append("Trading-sensitive project work requires trading architecture, data-science, and risk review.")
    return {
        "mode": EXPERT_WORKFLOW_MODE_PM_LED,
        "coordinator": _agent_profile_snapshot(coordinator),
        "summary": f"PM dispatched {len(threads)} expert thread(s) and synthesized their approval-readiness constraints.",
        "decisions": decisions,
        "blockers": blockers,
        "safety_gates": safety_gates,
        "next_action": (
            "Resolve specialist blockers before approval."
            if blockers
            else "Review the PM synthesis and approve the parent plan when ready; no files change before approval."
        ),
        "architect_review": {
            "status": (review or {}).get("status"),
            "score": (review or {}).get("score"),
            "blocking_reason": (review or {}).get("blocking_reason"),
        },
        "status": run.status,
    }


def _sync_expert_workflow_threads(
    db: Session,
    run: ProjectAutonomyRun,
    repo: CodeRepo,
    *,
    plan: dict[str, Any],
    files: list[dict[str, Any]],
    review: dict[str, Any] | None,
    commit: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    bootstrap_agent_profiles(db, repo_id=int(repo.id), user_id=run.user_id)
    coordinator = _agent_profile_by_key(
        db,
        repo_id=int(repo.id),
        user_id=run.user_id,
        profile_key=PM_COORDINATOR_PROFILE_KEY,
    )
    if coordinator is not None:
        run.agent_profile_id = coordinator.id
        run.agent_snapshot_json = _json_text(_agent_profile_snapshot(coordinator))
    keys = _route_expert_profile_keys(run.prompt, files)
    profile_by_key = {
        profile.profile_key: profile
        for profile in _agent_profile_query(db, user_id=run.user_id, repo_id=int(repo.id)).all()
    }
    threads: list[dict[str, Any]] = []
    for key in keys:
        profile = profile_by_key.get(key)
        if profile is None:
            continue
        thread = _build_expert_thread(
            run=run,
            profile=profile,
            profile_key=key,
            all_keys=keys,
            plan=plan,
            files=files,
            review=review,
        )
        thread["child_run_id"] = _upsert_expert_child_run(
            db,
            parent=run,
            child_profile=profile,
            thread=thread,
        )
        threads.append(thread)
    run.agents_json = _json_text(threads)
    synthesis = _pm_synthesis_for_threads(
        run=run,
        coordinator=coordinator,
        threads=threads,
        review=review,
    )
    _add_artifact(
        db,
        run.run_id,
        "pm_synthesis",
        "pm_synthesis",
        content_json=synthesis,
        commit=False,
    )
    _record_step(
        db,
        run,
        STAGE_COORDINATE,
        "PM dispatched expert threads",
        status="completed",
        agent_name=PM_COORDINATOR_PROFILE_KEY,
        detail={"threads": threads, "synthesis": synthesis},
        commit=False,
    )
    _record_message(
        db,
        run,
        "assistant",
        str(synthesis.get("summary") or "PM expert workflow is ready."),
        message_type="pm_synthesis",
        metadata={"expert_threads": threads, "pm_synthesis": synthesis},
        commit=False,
    )
    if commit:
        db.commit()
    return threads, synthesis


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
    selected_option = _selected_open_ended_option(prompt)
    if selected_option and rel == selected_option["path"]:
        return f"The operator selected option {selected_option['index']}: {selected_option['reason']}"
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
    selected_option = _selected_open_ended_option(prompt)
    if selected_option:
        return {selected_option["path"]}
    if _is_autopilot_cockpit_request(prompt):
        return {DESKTOP_AUTOPILOT_COCKPIT_FILE}
    if _has_any_token(prompt_lower, BROAD_DESKTOP_DETAIL_TOKENS) and not _has_broad_desktop_plan_override(prompt_lower):
        return {DESKTOP_API_CLIENT_FILE, DESKTOP_NETWORK_ERROR_FILE}
    if _is_broad_desktop_enhancement_request(prompt):
        return set(DESKTOP_AUTOPILOT_PLAN_FILES)
    return set()


def _architect_alternatives(context: dict[str, Any], repo_path: Path | None, prompt: str, selected: list[str]) -> list[dict[str, Any]]:
    if _requires_operator_choice_before_plan(prompt):
        return [
            dict(option)
            for option in OPEN_ENDED_ENHANCEMENT_OPTIONS
            if _candidate_exists(repo_path, str(option.get("path") or ""))
        ]
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
    if _requires_operator_choice_before_plan(prompt):
        blockers.append(ARCHITECT_REVIEW_BLOCKER_OPERATOR_CHOICE_REQUIRED)

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
        "scope_fingerprint": _architect_review_scope_fingerprint(prompt, plan),
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
        if ARCHITECT_REVIEW_BLOCKER_OPERATOR_CHOICE_REQUIRED in blockers:
            blocking_reason = OPEN_ENDED_ENHANCEMENT_BLOCKING_REASON
            critique["summary"] = "The architect needs the operator to choose a concrete enhancement direction."
            critique["next_action"] = "ask_operator_to_choose"
        else:
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
    critique = dict(review.get("critique") or {})
    if not critique.get("scope_fingerprint"):
        critique["scope_fingerprint"] = _architect_review_scope_fingerprint(
            run.prompt,
            _json_load(run.plan_json, {}),
        )
    row = ProjectAutonomyArchitectReview(
        run_id=run.run_id,
        attempt_index=int(review.get("attempt_index") or 1),
        status=str(review.get("status") or ARCHITECT_REVIEW_STATUS_FAILED),
        score=int(review.get("score") or 0),
        confidence=str(review.get("confidence") or "low"),
        dimensions_json=_json_text(review.get("dimensions") or {}),
        alternatives_json=_json_text(review.get("alternatives") or []),
        critique_json=_json_text(critique),
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
        review.get("stale") is not True
        and str(review.get("status") or "") == ARCHITECT_REVIEW_STATUS_PASSED
        and int(review.get("score") or 0) >= ARCHITECT_REVIEW_PASSING_SCORE
    )


def _architect_review_blockers(review: dict[str, Any] | None) -> list[str]:
    critique = (review or {}).get("critique") or {}
    raw = critique.get("blockers") or []
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if str(item or "").strip()]


def _architect_review_requires_operator_choice(review: dict[str, Any] | None) -> bool:
    return ARCHITECT_REVIEW_BLOCKER_OPERATOR_CHOICE_REQUIRED in set(_architect_review_blockers(review))


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
    blockers = set(_architect_review_blockers(review))
    if ARCHITECT_REVIEW_BLOCKER_OPERATOR_CHOICE_REQUIRED in blockers:
        return None
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
    if _architect_review_requires_operator_choice(review):
        alternatives = [
            f"{index}. {item.get('path')}: {item.get('reason')}"
            for index, item in enumerate(
                (review.get("alternatives") or [])[:OPEN_ENDED_OPTION_LIMIT],
                start=1,
            )
            if item.get("path") and item.get("reason")
        ]
        options = "\n".join(f"- {item}" for item in alternatives)
        suffix = f"\n\nGood next directions:\n{options}" if options else ""
        return (
            "I can help find and implement a small improvement, but I should not pick an arbitrary file "
            "and call it a plan. Tell me which user-visible area you want improved, or choose an option number from the "
            f"directions below, and I will draft a concrete reviewed plan before touching files.{suffix}"
        )
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
    agent_names = [
        str(item.get("display_name") or item.get("name") or "")
        for item in agents
        if item.get("display_name") or item.get("name")
    ]
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
        parts.append("Expert threads: " + ", ".join(agent_names[:6]) + ".")
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
    if review_passed:
        parts.append("Send feedback to revise the plan, or approve it to let me implement in an isolated worktree.")
    else:
        parts.append("Send feedback with the behavior or direction you want. I won't enable approval until the architect quality gate passes.")
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
    if _looks_like_greeting_or_chat(prompt):
        return (
            "Hey, I'm here. We can brainstorm, inspect ideas, or shape a plan together. "
            "I won't scan or edit the repo until you start a plan."
        )
    return (
        "I'm ready to help shape this. We can talk it through here first; when you want me "
            f"to inspect the repo and draft an implementation plan, use {PLAN_START_CHAT_ACTION_LABEL} in the sidebar."
    )


def _mechanical_autopilot_chat_reply(
    db: Session,
    run: ProjectAutonomyRun,
    latest_user_message: str,
) -> str | None:
    lower = re.sub(r"\s+", " ", (latest_user_message or "").strip().lower())
    if not lower:
        return None
    normalized = lower.strip(" .!?,")

    is_how_to = any(
        marker in lower
        for marker in (
            "how do i",
            "how can i",
            "where do i",
            "where can i",
            "can i",
            "can you show me",
            "what should i do",
            "what happens next",
            "next step",
        )
    )
    if is_how_to and any(
        marker in lower
        for marker in ("image", "screenshot", "attachment", "attach", "photo", "picture", "upload")
    ):
        return (
            "Use the attachment control beside the Autopilot chat composer to add a local image or screenshot. "
            "I will keep the file as prompt context for this run, and unsafe or remote-looking sources are blocked."
        )
    if is_how_to and any(marker in lower for marker in ("cancel", "stop", "pause")):
        return (
            "Use the cockpit cancel or stop action to halt this run. If work has already started, I keep the "
            "run record and evidence so the next step is auditable."
        )
    if is_how_to and any(marker in lower for marker in ("start", "plan", "begin", "kick off", "run")):
        return (
            f"Use {PLAN_START_CHAT_ACTION_LABEL} when you want me to inspect the repo and draft a plan. "
            "I will stay in chat mode until then, so casual questions do not trigger repo work."
        )
    if is_how_to and any(marker in lower for marker in ("approve", "merge", "finish", "ship")):
        return (
            "Review the drafted plan and validation notes first. When the cockpit shows an approval-ready plan, "
            "use the approve action there; merge stays gated until validation passes."
        )

    if any(marker in lower for marker in ("what files", "which files", "files changed", "changed files")):
        return _autopilot_changed_files_reply(db, run)
    if any(
        marker in lower
        for marker in (
            "what tests",
            "which tests",
            "tests ran",
            "test results",
            "validation",
            "validated",
        )
    ):
        return _autopilot_validation_reply(db, run)
    if any(marker in lower for marker in ("artifact", "artifacts", "evidence", "audit trail", "proof")):
        return _autopilot_evidence_reply(db, run)

    if any(
        marker in lower
        for marker in (
            "implement",
            "change",
            "fix",
            "add",
            "update",
            "build",
            "refactor",
            "code",
        )
    ):
        return None

    if normalized in {"help", "commands", "what can you do", "what can i ask"}:
        return _autopilot_command_help()
    if any(marker in lower for marker in ("status", "state", "where are we", "what stage", "progress")):
        return _autopilot_command_status(db, run)
    if any(marker in lower for marker in ("which agents", "list agents", "show agents", "agent status")):
        return _autopilot_command_agents(db, run, user_id=run.user_id)
    if any(marker in lower for marker in ("model policy", "which model", "local model", "model are you")):
        profile = _agent_profile_for_run(db, run)
        if profile is None:
            return f"This run is using model policy {run.model_policy or 'local_first'}."
        prompt_setting = _json_load(profile.prompt_setting_json, {})
        if not isinstance(prompt_setting, dict):
            prompt_setting = {}
        source = str(prompt_setting.get("source") or "generated profile")
        return (
            f"{profile.name} uses model policy {profile.model_policy or 'local_first'} "
            f"from {source}."
        )
    if any(marker in lower for marker in ("doctor", "readiness", "health check", "agent os")):
        return _autopilot_command_doctor(db, run, user_id=run.user_id)
    if any(marker in lower for marker in ("quality guard", "guardrails", "safety gates", "quality gates")):
        return (
            "Local model quality guardrails: plans must pass the architect gate before approval, "
            "scheduled-agent reports are checked for unsupported claims, generated agents default "
            "to observe/research/plan permissions, and implementation remains approval-first with "
            "validation and merge gates."
        )
    if any(marker in lower for marker in ("pending question", "operator question", "questions pending")):
        return _autopilot_command_questions(db, run)
    return None


def _chat_reply(db: Session, run: ProjectAutonomyRun, latest_user_message: str) -> str:
    if _looks_like_greeting_or_chat(latest_user_message):
        return _initial_chat_reply(latest_user_message)
    mechanical_reply = _mechanical_autopilot_chat_reply(db, run, latest_user_message)
    if mechanical_reply:
        return mechanical_reply
    if any(token in latest_user_message.lower() for token in ("implement", "change", "fix", "add", "update", "build")):
        return (
            "That sounds implementation-shaped. I can keep brainstorming here, or you can use "
            f"{PLAN_START_CHAT_ACTION_LABEL} in the sidebar when you want me to scan the repo and draft a safe plan."
        )
    model_info = select_local_model()
    if not model_info.get("model"):
        return (
            "I'm with you. We can keep shaping the idea here; local model chat is unavailable, "
            "but planning and implementation can still use the repo-aware Autopilot flow when you start a plan."
        )
    recent = (
        db.query(ProjectAutonomyMessage)
        .filter(ProjectAutonomyMessage.run_id == run.run_id)
        .order_by(ProjectAutonomyMessage.id.desc())
        .limit(8)
        .all()
    )
    messages = [
        {
            "role": "system",
            "content": (
                "You are CHILI, a local project architect chat. This is a brainstorming conversation, "
                "not an implementation run. Be concise, warm, and useful. Do not claim you changed files. "
                "If implementation is needed, tell the user to start a plan."
            ),
        }
    ]
    for row in reversed(recent):
        role = "assistant" if row.role == "assistant" else "user"
        content = row.content
        if role == "user":
            attachment_context = _attachment_context(_message_attachments_from_metadata(row.metadata_json))
            if attachment_context:
                content = f"{content}\n{attachment_context}"
        messages.append({"role": role, "content": content})
    result = ollama_client.chat(
        messages,
        str(model_info["model"]),
        temperature=0.35,
        timeout_sec=45,
        options={"num_predict": 220, "num_ctx": 2048, "keep_alive": _OLLAMA_KEEP_ALIVE},
    )
    _note_model_call_result(str(model_info["model"]), result)
    _add_artifact(
        db,
        run.run_id,
        "model_call",
        "chat_model_call",
        content_json={
            "model": model_info["model"],
            "ok": result.ok,
            "latency_ms": result.latency_ms,
            "error": result.error,
            "skipped_models": model_info.get("skipped_models"),
            "purpose": "brainstorm_chat",
        },
        commit=False,
    )
    if result.ok and result.text.strip():
        return _clip(result.text.strip(), CHAT_REPLY_LIMIT)
    return (
        "I'm here for the brainstorming. The local chat model didn't answer cleanly, "
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
    option_plan = _open_ended_option_plan(prompt, repo_path)
    if option_plan is not None:
        return option_plan
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
        prompt = _build_edit_prompt(rel, content or "", desc, conventions)
        result = ollama_client.chat(
            [
                {"role": "system", "content": "Return a single unified diff. No prose."},
                {"role": "user", "content": prompt},
            ],
            str(model_info["model"]),
            temperature=0.1,
            timeout_sec=_EDIT_TIMEOUT_SEC,
            options={
                "num_predict": _EDIT_NUM_PREDICT,
                "num_ctx": _EDIT_NUM_CTX,
                "keep_alive": _OLLAMA_KEEP_ALIVE,
            },
        )
        _note_model_call_result(str(model_info["model"]), result)
        _add_artifact(
            db,
            run.run_id,
            "model_call",
            f"edit_{rel}",
            content_json={
                "model": model_info["model"],
                "file": rel,
                "ok": result.ok,
                "latency_ms": result.latency_ms,
                "error": result.error,
                "skipped_models": model_info.get("skipped_models"),
                "prompt_chars": len(prompt),
                "timeout_sec": _EDIT_TIMEOUT_SEC,
                "num_predict": _EDIT_NUM_PREDICT,
                "num_ctx": _EDIT_NUM_CTX,
                "keep_alive": _OLLAMA_KEEP_ALIVE,
            },
        )
        _check_cancel(db, run)
        if not result.ok:
            rejections.append(f"{rel}: model call failed ({result.error or 'unknown error'})")
            if try_fallback(rel, content):
                continue
            continue
        diff = _extract_diff(result.text)
        if not diff:
            reason = "model_response_missing_unified_diff"
            _add_artifact(
                db,
                run.run_id,
                "diff_rejected",
                rel,
                content_json={"reason": reason, "response_preview": _clip(result.text, 800)},
            )
            rejections.append(f"{rel}: model did not return a unified diff")
            if try_fallback(rel, content):
                continue
            continue
        validity = _validate_diff(diff, rel, content)
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
    return {
        "step_key": result.step_key,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "skipped": result.skipped,
        "skip_reason": result.skip_reason,
        "passed": result.exit_code == 0,
    }


def run_validation(worktree: Path, changed_files: list[str]) -> list[dict[str, Any]]:
    results: list[StepResult] = [
        run_ast_syntax(worktree),
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
        if _architect_review_requires_operator_choice(review):
            break
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

    if run.autonomy_level == AUTONOMY_LEVEL_SCHEDULED_AGENT:
        agents = assign_agent_lanes(files)
        pm_synthesis: dict[str, Any] = {}
        run.agents_json = _json_text(agents)
        _record_step(
            db,
            run,
            STAGE_ASSIGN_ROLES,
            "Agent assigned implementation lanes",
            status="completed",
            detail={"agents": agents},
            commit=False,
        )
    else:
        agents, pm_synthesis = _sync_expert_workflow_threads(
            db,
            run,
            repo,
            plan=plan,
            files=files,
            review=review,
            commit=False,
        )
        _record_step(
            db,
            run,
            STAGE_ASSIGN_ROLES,
            "PM assigned expert threads",
            status="completed",
            agent_name=PM_COORDINATOR_PROFILE_KEY,
            detail={"agents": agents, "pm_synthesis": pm_synthesis},
            commit=False,
        )
    _record_message(
        db,
        run,
        "assistant",
        _plan_message(plan, files, agents, review),
        message_type="plan",
        metadata={"plan": plan, "files": files, "agents": agents, "architect_review": review, "pm_synthesis": pm_synthesis},
        commit=False,
    )
    if review.get("status") != ARCHITECT_REVIEW_STATUS_PASSED:
        clarification = _clarification_message(review)
        run.status = RUN_STATUS_AWAITING_CLARIFICATION
        run.current_stage = STAGE_ARCHITECT_REVIEW
        run.plan_status = PLAN_STATUS_AWAITING_CLARIFICATION
        run.updated_at = _utcnow()
        record_operator_question(
            db,
            run,
            clarification,
            context={"architect_review": review, "plan": plan},
            commit=False,
        )
        _record_message(
            db,
            run,
            "assistant",
            clarification,
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


def _scheduled_agent_permissions(run: ProjectAutonomyRun) -> dict[str, Any]:
    snapshot = _json_load(run.agent_snapshot_json, {})
    permissions = snapshot.get("permissions") if isinstance(snapshot, dict) else {}
    if not isinstance(permissions, dict):
        permissions = {}
    merged = dict(DEFAULT_AGENT_PERMISSIONS)
    merged.update({key: bool(value) for key, value in permissions.items()})
    return merged


def _should_run_scheduled_report_only(run: ProjectAutonomyRun) -> bool:
    if run.autonomy_level != AUTONOMY_LEVEL_SCHEDULED_AGENT:
        return False
    permissions = _scheduled_agent_permissions(run)
    return not bool(permissions.get(AGENT_PERMISSION_WORKTREE))


def _scheduled_agent_profile_snapshot(run: ProjectAutonomyRun) -> dict[str, Any]:
    snapshot = _json_load(run.agent_snapshot_json, {})
    return snapshot if isinstance(snapshot, dict) else {}


def _scheduled_agent_report_prompt(
    run: ProjectAutonomyRun,
    repo: CodeRepo,
    context: dict[str, Any],
) -> str:
    snapshot = _scheduled_agent_profile_snapshot(run)
    prompt_setting = snapshot.get("prompt_setting") if isinstance(snapshot.get("prompt_setting"), dict) else {}
    system_prompt = str(prompt_setting.get("system_prompt") or "")
    profile_name = str(snapshot.get("name") or "Scheduled Agent")
    profile_key = str(snapshot.get("profile_key") or "")
    tier = str(snapshot.get("tier") or "")
    role = str(snapshot.get("role") or "")
    files = []
    for item in (context.get("relevant_files") or [])[:8]:
        if isinstance(item, dict):
            files.append(str(item.get("file") or ""))
    insights = [
        str(item.get("description") or "")
        for item in (context.get("insights") or [])[:6]
        if isinstance(item, dict) and item.get("description")
    ]
    parts = [
        "Return one compact JSON object for a scheduled local agent status report.",
        "No markdown. No prose outside JSON. Do not claim files changed.",
        "This agent is plan-only: observe, research, summarize, and ask questions only.",
        "",
        f"Agent: {profile_name} key={profile_key} tier={tier} role={role}",
        "",
        "Agent operating prompt:",
        _clip(system_prompt, 2600),
        "",
        "Scheduled request:",
        run.prompt,
        "",
        f"Repo: {repo.name or repo.path} path={repo.path}",
    ]
    if files:
        parts.extend(["", "Relevant files:"])
        parts.extend(f"- {path}" for path in files if path)
    if insights:
        parts.extend(["", "Known repo signals:"])
        parts.extend(f"- {_clip(item, 220)}" for item in insights)
    parts.extend(
        [
            "",
            "JSON shape:",
            '{"summary":"<=30 words","findings":["<=18 words"],"recommended_next_steps":["<=18 words"],"operator_question":""}',
            "If operator input is not needed, operator_question must be empty.",
        ]
    )
    return _clip("\n".join(parts), _PLAN_PROMPT_CHAR_LIMIT)


def _fallback_scheduled_agent_report(
    run: ProjectAutonomyRun,
    repo: CodeRepo,
    reason: str,
) -> dict[str, Any]:
    snapshot = _scheduled_agent_profile_snapshot(run)
    name = str(snapshot.get("name") or "Scheduled agent")
    prompt_setting = snapshot.get("prompt_setting") if isinstance(snapshot.get("prompt_setting"), dict) else {}
    source = str(prompt_setting.get("source") or "generated_profile")
    return {
        "summary": f"{name} completed a plan-only scheduled pass for {repo.name or repo.path}.",
        "findings": [
            "No files were changed.",
            "This agent is still limited to observation, research, summaries, and operator questions.",
            f"Local report generation used deterministic fallback: {_clip(_friendly_model_issue(reason), 120)}",
        ],
        "recommended_next_steps": [
            "Review this agent's role prompt and schedule before enabling patch permissions.",
            "Use the inspector to grant worktree permission only for agents you trust to draft patches.",
        ],
        "operator_question": "",
        "source": source,
    }


def _normalise_scheduled_agent_report(raw: dict[str, Any]) -> dict[str, Any]:
    summary = str(raw.get("summary") or "").strip()
    findings = _string_items(raw.get("findings"), limit=5)
    next_steps = _string_items(raw.get("recommended_next_steps"), limit=5)
    question = str(raw.get("operator_question") or "").strip()
    if not summary:
        summary = "Scheduled agent completed a plan-only observation cycle."
    if not findings:
        findings = ["No files were changed."]
    if not next_steps:
        next_steps = ["Keep this agent plan-only until the operator enables patch permissions."]
    return {
        "summary": _clip(summary, 500),
        "findings": [_clip(item, 260) for item in findings],
        "recommended_next_steps": [_clip(item, 260) for item in next_steps],
        "operator_question": _clip(question, 500),
    }


def _scheduled_agent_report_text(report: dict[str, Any]) -> str:
    parts = [
        str(report.get("summary") or ""),
        *[str(item) for item in _string_items(report.get("findings"), limit=8)],
        *[str(item) for item in _string_items(report.get("recommended_next_steps"), limit=8)],
        str(report.get("operator_question") or ""),
    ]
    return "\n".join(parts).lower()


def _scheduled_agent_report_has_false_action_claim(report: dict[str, Any]) -> bool:
    text = _scheduled_agent_report_text(report)
    if not text:
        return False
    for marker in SCHEDULED_AGENT_REPORT_FALSE_ACTION_MARKERS:
        if marker in text:
            return True
    return False


def _scheduled_agent_report_mentions_plan_only(report: dict[str, Any]) -> bool:
    text = _scheduled_agent_report_text(report)
    return any(marker in text for marker in SCHEDULED_AGENT_REPORT_NO_CHANGE_MARKERS)


def _scheduled_agent_report_quality(
    report: dict[str, Any],
    *,
    repaired: bool = False,
    initial_quality: dict[str, Any] | None = None,
) -> dict[str, Any]:
    issues: list[str] = []
    score = 100
    summary = str(report.get("summary") or "").strip()
    findings = _string_items(report.get("findings"), limit=8)
    next_steps = _string_items(report.get("recommended_next_steps"), limit=8)
    question = str(report.get("operator_question") or "").strip()
    if len(summary.split()) < 5:
        score -= 25
        issues.append("summary_too_thin")
    if len([item for item in findings if len(item.split()) >= 3]) < 2:
        score -= 15
        issues.append("findings_too_thin")
    if not next_steps:
        score -= 20
        issues.append("missing_next_steps")
    if not _scheduled_agent_report_mentions_plan_only(report):
        score -= 15
        issues.append("missing_plan_only_disclaimer")
    if _scheduled_agent_report_has_false_action_claim(report):
        score -= 45
        issues.append("false_action_claim")
    if question and not question.endswith("?"):
        score -= 5
        issues.append("operator_question_not_question")
    score = max(0, min(100, score))
    status = (
        SCHEDULED_AGENT_REPORT_QUALITY_PASSED
        if score >= SCHEDULED_AGENT_REPORT_QUALITY_PASSING_SCORE and "false_action_claim" not in issues
        else SCHEDULED_AGENT_REPORT_QUALITY_LOW
    )
    if repaired and status == SCHEDULED_AGENT_REPORT_QUALITY_PASSED:
        status = SCHEDULED_AGENT_REPORT_QUALITY_REPAIRED
    quality: dict[str, Any] = {
        "status": status,
        "score": score,
        "passing_score": SCHEDULED_AGENT_REPORT_QUALITY_PASSING_SCORE,
        "issues": issues,
        "repaired": repaired,
    }
    if initial_quality:
        quality["initial_score"] = initial_quality.get("score")
        quality["initial_issues"] = initial_quality.get("issues") or []
        quality["initial_status"] = initial_quality.get("status")
    return quality


def _repair_scheduled_agent_report(
    run: ProjectAutonomyRun,
    repo: CodeRepo,
    report: dict[str, Any],
    quality: dict[str, Any],
) -> dict[str, Any]:
    repaired = _normalise_scheduled_agent_report(
        _fallback_scheduled_agent_report(
            run,
            repo,
            "scheduled agent report quality gate rejected the local model response",
        )
    )
    question = str(report.get("operator_question") or "").strip()
    if question:
        repaired["operator_question"] = question
    repaired["findings"] = [
        "No files were changed.",
        "The first local report was repaired by the scheduled-agent quality guard.",
        "This cycle stayed inside observe, research, summarize, and question permissions.",
    ]
    repaired["recommended_next_steps"] = [
        "Review the repaired report before enabling patch permissions for this agent.",
        "Tune the agent operating prompt if repeated reports need repair.",
    ]
    repaired["repair_reason"] = ", ".join(str(item) for item in quality.get("issues") or [])
    return repaired


def _quality_checked_scheduled_agent_report(
    run: ProjectAutonomyRun,
    repo: CodeRepo,
    report: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    normalised = _normalise_scheduled_agent_report(report)
    initial_quality = _scheduled_agent_report_quality(normalised)
    if initial_quality["status"] == SCHEDULED_AGENT_REPORT_QUALITY_PASSED:
        normalised["quality"] = initial_quality
        return normalised, initial_quality
    repaired = _repair_scheduled_agent_report(run, repo, normalised, initial_quality)
    repaired = _normalise_scheduled_agent_report(repaired)
    final_quality = _scheduled_agent_report_quality(
        repaired,
        repaired=True,
        initial_quality=initial_quality,
    )
    repaired["quality"] = final_quality
    return repaired, final_quality


def _parse_scheduled_agent_report_json(reply: str) -> dict[str, Any] | None:
    text = (reply or "").strip()
    if not text:
        return None
    candidates = [text]
    fenced = re.search(r"```(?:json)?\s*\n(\{.*?\})\s*\n```", text, re.DOTALL)
    if fenced:
        candidates.insert(0, fenced.group(1).strip())
    first = text.find("{")
    last = text.rfind("}")
    if first >= 0 and last > first:
        candidates.append(text[first : last + 1])
    for candidate in candidates:
        try:
            decoded = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            return decoded
    return None


def _string_items(raw: Any, *, limit: int) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _scheduled_agent_report_message(report: dict[str, Any], run: ProjectAutonomyRun) -> str:
    snapshot = _scheduled_agent_profile_snapshot(run)
    name = str(snapshot.get("name") or "Scheduled agent")
    parts = [f"{name} scheduled cycle report", "", str(report.get("summary") or "")]
    findings = _string_items(report.get("findings"), limit=5)
    if findings:
        parts.extend(["", "Findings:"])
        parts.extend(f"- {item}" for item in findings)
    next_steps = _string_items(report.get("recommended_next_steps"), limit=5)
    if next_steps:
        parts.extend(["", "Recommended next steps:"])
        parts.extend(f"- {item}" for item in next_steps)
    question = str(report.get("operator_question") or "").strip()
    if question:
        parts.extend(["", f"Needs operator input: {question}"])
    parts.extend(["", "No files were changed in this plan-only cycle."])
    quality = report.get("quality") if isinstance(report.get("quality"), dict) else {}
    if quality:
        status = str(quality.get("status") or "checked").replace("_", " ")
        score = quality.get("score")
        score_text = f" ({score}/100)" if score is not None else ""
        parts.append(f"Quality guard: {status}{score_text}.")
    return "\n".join(parts)


def _build_scheduled_agent_report(
    db: Session,
    run: ProjectAutonomyRun,
    repo: CodeRepo,
) -> dict[str, Any]:
    try:
        context = _gather_context(db, int(repo.id), run.prompt, user_id=run.user_id)
    except Exception:
        context = {"repos": [], "insights": [], "hotspots": [], "relevant_files": []}
    prompt = _scheduled_agent_report_prompt(run, repo, context)
    model_info = select_local_model()
    if not model_info.get("model"):
        report, quality = _quality_checked_scheduled_agent_report(
            run,
            repo,
            _fallback_scheduled_agent_report(
                run,
                repo,
                str(model_info.get("recommendation") or "No local Ollama model is available."),
            ),
        )
        _add_artifact(
            db,
            run.run_id,
            SCHEDULED_AGENT_REPORT_QUALITY_ARTIFACT_TYPE,
            SCHEDULED_AGENT_REPORT_QUALITY_ARTIFACT_NAME,
            content_json=quality,
            commit=False,
        )
        _add_artifact(
            db,
            run.run_id,
            SCHEDULED_AGENT_REPORT_ARTIFACT_TYPE,
            SCHEDULED_AGENT_REPORT_ARTIFACT_NAME,
            content_json={**report, "model_selection": model_info, "prompt_preview": _clip(prompt, 1200)},
            commit=False,
        )
        return report
    messages = [
        {
            "role": "system",
            "content": (
                "You are a local scheduled CHILI agent. Follow the agent operating prompt. "
                "Produce a truthful plan-only status report. Do not claim edits, commits, tests, "
                "or external actions unless explicitly provided in context."
            ),
        },
        {"role": "user", "content": prompt},
    ]
    result = ollama_client.chat(
        messages,
        str(model_info["model"]),
        temperature=0.2,
        timeout_sec=min(_PLAN_TIMEOUT_SEC, 45),
        options={"num_predict": 220, "num_ctx": _PLAN_NUM_CTX, "keep_alive": _OLLAMA_KEEP_ALIVE},
    )
    _note_model_call_result(str(model_info["model"]), result)
    _add_artifact(
        db,
        run.run_id,
        "model_call",
        "scheduled_agent_report_model_call",
        content_json={
            "model": model_info["model"],
            "ok": result.ok,
            "latency_ms": result.latency_ms,
            "error": result.error,
            "skipped_models": model_info.get("skipped_models"),
            "purpose": "scheduled_agent_report",
            "prompt_chars": len(prompt),
        },
        commit=False,
    )
    parsed = _parse_scheduled_agent_report_json(result.text) if result.ok else None
    if not parsed:
        parsed = _fallback_scheduled_agent_report(run, repo, result.error or "local model returned unusable report")
    report, quality = _quality_checked_scheduled_agent_report(run, repo, parsed)
    _add_artifact(
        db,
        run.run_id,
        SCHEDULED_AGENT_REPORT_QUALITY_ARTIFACT_TYPE,
        SCHEDULED_AGENT_REPORT_QUALITY_ARTIFACT_NAME,
        content_json=quality,
        commit=False,
    )
    _add_artifact(
        db,
        run.run_id,
        SCHEDULED_AGENT_REPORT_ARTIFACT_TYPE,
        SCHEDULED_AGENT_REPORT_ARTIFACT_NAME,
        content_json={**report, "prompt_preview": _clip(prompt, 1200)},
        commit=False,
    )
    return report


def _run_scheduled_agent_report_phase(
    db: Session,
    run: ProjectAutonomyRun,
    repo: CodeRepo,
    repo_path: Path,
) -> dict[str, Any]:
    run.status = RUN_STATUS_RUNNING
    run.current_stage = STAGE_REPO_SCAN
    run.plan_status = PLAN_STATUS_DRAFTING
    if run.started_at is None:
        run.started_at = _utcnow()
    db.commit()
    snapshot = _scheduled_agent_profile_snapshot(run)
    agent_name = str(snapshot.get("profile_key") or snapshot.get("name") or "scheduled_agent")
    _record_step(
        db,
        run,
        STAGE_CLASSIFY,
        "Scheduled agent cycle started",
        status="completed",
        agent_name=agent_name,
        detail={"agent": snapshot, "mode": "plan_only"},
    )
    _record_step(
        db,
        run,
        STAGE_REPO_SCAN,
        "Scheduled agent inspected repository context",
        status="completed",
        agent_name=agent_name,
        detail={"repo": repo.name, "path": str(repo_path)},
    )
    report = _build_scheduled_agent_report(db, run, repo)
    run.plan_json = _json_text({"analysis": report["summary"], "files": [], "notes": "Plan-only scheduled report."})
    run.files_json = _json_text([])
    run.agents_json = _json_text([{"name": agent_name, "role": snapshot.get("role"), "status": "reported", "files": []}])
    _record_message(
        db,
        run,
        "assistant",
        _scheduled_agent_report_message(report, run),
        message_type="agent_cycle_report",
        metadata={"report": report, "agent": snapshot},
        commit=False,
    )
    if report.get("operator_question"):
        record_operator_question(
            db,
            run,
            str(report.get("operator_question")),
            context={"report": report, "agent": snapshot},
            commit=False,
        )
        run.status = RUN_STATUS_AWAITING_CLARIFICATION
        run.current_stage = STAGE_COORDINATE
        run.plan_status = PLAN_STATUS_AWAITING_CLARIFICATION
        run.merge_status = "not_applicable"
        run.merge_message = "Scheduled plan-only cycle needs operator input. No files were changed."
        run.updated_at = _utcnow()
        db.commit()
        db.refresh(run)
        return run_payload(db, run, include_events=True)
    run.plan_status = PLAN_STATUS_IMPLEMENTED
    run.updated_at = _utcnow()
    db.commit()
    return run_payload(
        db,
        _finish(
            db,
            run,
            status=RUN_STATUS_COMPLETED,
            stage=STAGE_COORDINATE,
            title="Scheduled agent cycle reported",
            merge_status="not_applicable",
            merge_message="Scheduled plan-only cycle completed. No files were changed.",
        ),
        include_events=True,
    )


def _run_implementation_phase(db: Session, run: ProjectAutonomyRun, repo: CodeRepo, repo_path: Path) -> dict[str, Any]:
    plan = _json_load(run.plan_json, {})
    validation: list[dict[str, Any]] = []
    files = _plan_files(plan)
    if not files:
        raise AutonomyBlocked("The approved plan does not identify concrete files to change.")
    review_blocker = _approved_plan_review_blocker(db, run, plan, action="implementation")
    if review_blocker:
        raise AutonomyBlocked(review_blocker)
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
        if _should_run_scheduled_report_only(run):
            return _run_scheduled_agent_report_phase(db, run, repo, repo_path)
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
