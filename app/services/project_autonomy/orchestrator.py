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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable
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
STAGE_PLAN = "plan"
STAGE_QUEUED = RUN_STATUS_QUEUED
STAGE_REPO_SCAN = "repo_scan"
STAGE_ASSIGN_ROLES = "assign_roles"
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
    if _looks_like_greeting_or_chat(prompt):
        return (
            "Hey, I'm here. We can brainstorm, inspect ideas, or shape a plan together. "
            "I won't scan or edit the repo until you start a plan."
        )
    return (
        "I'm ready to help shape this. We can talk it through here first; when you want me "
            f"to inspect the repo and draft an implementation plan, use {PLAN_START_CHAT_ACTION_LABEL} in the sidebar."
    )


def _chat_reply(db: Session, run: ProjectAutonomyRun, latest_user_message: str) -> str:
    if _looks_like_greeting_or_chat(latest_user_message):
        return _initial_chat_reply(latest_user_message)
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
