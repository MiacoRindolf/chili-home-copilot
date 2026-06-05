"""Stable material fingerprints for replay-safe LLM cache keys."""
from __future__ import annotations

import hashlib
import json
import re

from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_VOLATILE_MATERIAL_KEYS = {
    "accessed_at",
    "active_panel",
    "active_tab",
    "anchor_uuid",
    "api_duration_ms",
    "api_duration_seconds",
    "api_request_id",
    "abort_controller",
    "agent_id",
    "agent_name",
    "agent_uuid",
    "as_of",
    "automation_id",
    "auto_mode_active",
    "before_id",
    "cache_control",
    "cache_creation_input_tokens",
    "cache_creation_tokens",
    "cache_editing_header_latched",
    "cache_hit",
    "cache_key",
    "cache_read_input_tokens",
    "cache_read_tokens",
    "cache_safe_params",
    "cache_s",
    "cache_status",
    "cached_tokens",
    "cached_prompt_tokens",
    "agent_cycle_id",
    "attempt_id",
    "completed_at",
    "client_height",
    "client_message_id",
    "client_width",
    "completion_tokens",
    "control_request_id",
    "correlation_id",
    "cost_usd",
    "created_at",
    "current_time_iso",
    "crawled_at",
    "cycle_id",
    "delivery_id",
    "duration_ms",
    "duration_seconds",
    "display_path",
    "elapsed_ms",
    "elapsed_seconds",
    "event_id",
    "event_uuid",
    "estimated_cost_usd",
    "estimated_saved_usd",
    "fetch_time",
    "fetched_at",
    "fast_mode",
    "fast_mode_header_latched",
    "finished_at",
    "finished_monotonic",
    "generated_at",
    "generated_on",
    "generated_time",
    "generated_ts",
    "generated_utc",
    "gateway_log_id",
    "gateway_model",
    "gateway_request_id",
    "generation_request_id",
    "held_back_result",
    "head_uuid",
    "indexed_at",
    "inflight_promise",
    "invoking_request_id",
    "heartbeat_id",
    "last_seen",
    "last_observed_at",
    "last_refreshed",
    "last_refreshed_at",
    "last_refreshed_time",
    "last_refreshed_utc",
    "last_updated",
    "last_emitted",
    "last_heartbeat_age_seconds",
    "last_api_completion_timestamp",
    "last_request_id",
    "last_sequence_num",
    "leaf_uuid",
    "latency_ms",
    "latency_seconds",
    "llm_model",
    "message_id",
    "message_uuid",
    "messages_to_ack",
    "material_cache_key",
    "mtime_ms",
    "next_cursor",
    "operation_id",
    "original_chars",
    "originating_request_id",
    "observed_at",
    "output_file",
    "output_file_path",
    "output_tokens",
    "output_path",
    "paid_uncached_prompt_tokens",
    "parent_cache_reason",
    "parent_cache_state",
    "parent_cache_status",
    "parent_cache_suppress_reason",
    "parent_tool_use_id",
    "parent_uuid",
    "parent_uncached_token_count",
    "parent_uncached_tokens",
    "parent_pid",
    "pending_last_emitted_entry",
    "pending_suggestion",
    "pane_id",
    "pid",
    "process_id",
    "progress_uuid",
    "permission_request_id",
    "prompt_id",
    "prompt_cache_eligible",
    "prompt_cache_ttl",
    "prompt_tokens",
    "prompt_suggestion_state",
    "prompt_uuid",
    "provider_model",
    "premium_cost_usd",
    "request_id",
    "emitted_at",
    "request_epoch_ms",
    "request_latency_ms",
    "request_started_at",
    "request_uuid",
    "response_ms",
    "response_id",
    "response_time_ms",
    "retrieved_at",
    "retrieved_at_utc",
    "refreshed_at",
    "refreshed_time",
    "refreshed_utc",
    "report_generated_at",
    "report_generated_utc",
    "resolved_at",
    "replay_user_messages",
    "run_id",
    "scan_time",
    "scanned_at",
    "screen_height",
    "screen_width",
    "scroll_top",
    "scroll_y",
    "search_time",
    "searched_at",
    "selected_panel",
    "selected_tab",
    "market_data_retrieved_at_utc",
    "provider_time",
    "provider_time_utc",
    "quote_provider_time",
    "quote_provider_time_utc",
    "quote_retrieved_at",
    "quote_retrieved_at_utc",
    "snapshot_at",
    "source_thread_id",
    "seq",
    "sequence",
    "session_id",
    "session_url",
    "session_uuid",
    "service_tier",
    "started_at",
    "started_monotonic",
    "stream_request_id",
    "subagent_name",
    "suggestion_state",
    "summarizes_uuid",
    "tail_uuid",
    "target_thread_id",
    "task_id",
    "team_name",
    "teammate_name",
    "thread_id",
    "thinking_clear_latched",
    "timestamp",
    "time_since_last_api_call_ms",
    "trace_id",
    "transport_request_id",
    "from_sequence_num",
    "seen_sequence_nums",
    "tool_use_id",
    "tool_use_ids",
    "tool_result_id",
    "tool_result_ids",
    "tool_use_count",
    "total_tokens",
    "preceding_tool_use_ids",
    "tool_uses",
    "total_tool_use_count",
    "uncached_prompt_tokens",
    "tmux_pane_id",
    "ui_refresh_id",
    "ui_state_id",
    "updated_at",
    "user_message_id",
    "uuid",
    "viewport_height",
    "viewport_width",
    "wall_time_ms",
    "wall_time",
    "wall_time_seconds",
    "window_height",
    "window_width",
    "worker_color",
    "worker_name",
    "worker_pid",
    "worker_id",
    "work_id",
    "worktree_branch",
    "worktree_path",
}
_TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "msclkid",
    "utm_campaign",
    "utm_content",
    "utm_id",
    "utm_medium",
    "utm_source",
    "utm_term",
}
_PLAIN_ID_VOLATILE_ENVELOPE_KEYS = {
    "description",
    "input",
    "permission_suggestions",
    "status",
    "team_name",
    "tool_name",
    "tool_use_id",
    "worker_id",
    "worker_name",
}
_PROTOCOL_MESSAGE_VOLATILE_KEYS_BY_TYPE = {
    "permission_request": {"from", "id"},
    "permission_response": {"from", "id"},
    "sandbox_permission_request": {"from", "id"},
    "sandbox_permission_response": {"from", "id"},
    "plan_approval_request": {"from", "plan_file_path"},
    "plan_approval_response": {"from"},
    "shutdown_request": {"from"},
    "shutdown_approved": {"from", "backend_type"},
    "shutdown_rejected": {"from"},
    "task_assignment": {"assigned_by"},
    "team_permission_update": {"from"},
    "mode_set_request": {"from"},
    "agent_listing_delta": {"added_types", "is_initial"},
    "deferred_tools_delta": {"added_names"},
    "mcp_instructions_delta": {"added_names"},
    "output_token_usage": {"budget", "session", "turn"},
    "skill_discovery": {"signal", "source"},
    "skill_listing": {"is_initial", "skill_count"},
}
_TELEMETRY_ONLY_MATERIAL_TYPES = {
    "budget_usd",
    "command_permissions",
    "compaction_reminder",
    "context_efficiency",
    "context_window_status",
    "context_window_update",
    "dynamic_skill",
    "edited_image_file",
    "hook_cancelled",
    "hook_error_during_execution",
    "hook_non_blocking_error",
    "hook_permission_decision",
    "hook_system_message",
    "model_usage_delta",
    "output_token_usage",
    "structured_output",
    "token_budget_status",
    "token_usage",
    "usage_delta",
    "prompt_suggestion",
    "prompt_suggestion_lifecycle",
    "prompt_suggestion_state",
}
_HOOK_TYPES_WITH_VISIBLE_MATERIAL = {
    "hook_additional_context",
    "hook_blocking_error",
    "hook_stopped_continuation",
    "hook_success",
}
_DROP_MATERIAL = object()
_LABEL_RE = re.compile(r"^\s*([^:=]{1,64})\s*[:=]\s*(.*)$")
_TRANSCRIPT_ROLE_RE = re.compile(
    r"^\s*(?:user|assistant|system|developer|tool|human|ai|bot|agent)(?:[\s_-]+message)?\s*:\s*(.*)$",
    re.IGNORECASE,
)
_VOLATILE_PREFIX_RE = re.compile(
    r"^\s*[\[(]\s*([A-Za-z_][\w:.-]{0,63})\s*[:=][^\])]+[\])]\s*",
    re.IGNORECASE,
)
_XML_TAG_RE = re.compile(r"^\s*<\s*([A-Za-z_][\w:.-]{0,63})\b[^>]*>.*?</\s*\1\s*>\s*$")
_XML_LEAF_TAG_RE = re.compile(r"<\s*([A-Za-z_][\w:.-]{0,63})\b[^>]*>[^<]*</\s*\1\s*>")
_XML_ATTR_RE = re.compile(r"\s+([A-Za-z_][\w:.-]{0,63})\s*=\s*(\"[^\"]*\"|'[^']*')")
_CHILI_REPO_ROOT_ALIAS_RE = re.compile(
    r"(?i)\b(?:[CD]:[\\/]+dev[\\/]+chili-home-copilot|/mnt/[cd]/dev/chili-home-copilot)\b"
)
_TRUNCATED_FULL_OUTPUT_RE = re.compile(
    r"(?i)^\[?\s*truncated\.?\s+full\s+output\s*:\s+.+\]?\s*$"
)
_OUTPUT_TRUNCATED_SAVED_RE = re.compile(
    r"(?i)^output\s+truncated\s*\([^)]*\)\.\s*full\s+output\s+saved\s+to\s*:\s*.+$"
)
_PERSISTED_OUTPUT_SAVED_RE = re.compile(
    r"(?i)^output\s+too\s+large\s*\([^)]*\)\.\s*full\s+output\s+saved\s+to\s*:\s*.+$"
)
_PERSISTED_OUTPUT_PREVIEW_HEADER_RE = re.compile(
    r"(?i)^preview\s*\(\s*first\s+[\d,.]+\s*(?:b|bytes?|kb|mb|kib|mib)?\s*\)\s*:\s*$"
)
_TOOL_RESULT_CLEARED_RE = re.compile(r"(?i)^\[\s*old\s+tool\s+result\s+content\s+cleared\s*\]\s*$")
_OUTPUT_TRUNCATED_DISK_CAP_RE = re.compile(
    r"(?i)^\[?\s*output\s+truncated\s*:\s*exceeded\s+.+?\s+disk\s+cap\s*\]?\s*$"
)
_OUTPUT_TRUNCATED_REMOVED_RE = re.compile(
    r"(?i)\.\.\.\s*\[\s*output\s+truncated\s*-\s*[\d,.]+\s*(?:kb|mb|bytes?)\s+removed\s*\]"
)
_OUTPUT_TRUNCATED_EXCEEDED_CHARS_RE = re.compile(
    r"(?i)(?:\.\.\.|\u2026|\u00e2\u20ac\u00a6)\s*"
    r"\[\s*output\s+truncated\s*-\s*exceeded\s+[\d,.]+\s+characters\s*\]"
)
_PYTEST_SUMMARY_TIMING_RE = re.compile(
    r"(?i)^(\s*=*\s*(?=.*\b(?:passed|failed|errors?|skipped|xfailed|xpassed|warnings?|rerun)\b)"
    r"[A-Za-z0-9_.,:=\s-]+?)\s+in\s+[\d,.]+(?:ms|s|sec|secs|seconds?|m|min|mins|minutes?)"
    r"(\s*=*\s*)$"
)
_UNITTEST_SUMMARY_TIMING_RE = re.compile(
    r"(?i)^(\s*Ran\s+[\d,.]+\s+tests?)\s+in\s+[\d,.]+(?:ms|s|sec|secs|seconds?|m|min|mins|minutes?)(\s*)$"
)
_FLUTTER_TEST_ELAPSED_RE = re.compile(
    r"^\s*\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?\s+(\+\d+(?:\s+-\d+)?(?::|\s).*)$"
)
_TASK_OUTPUT_READ_FAILED_RE = re.compile(
    r"(?i)^TaskOutput\.\#readStdoutFromFile\s*:\s*failed\s+to\s+read\s+.+?\s+\([^)]+\)\s*:\s*.+$"
)
_BASH_OUTPUT_UNAVAILABLE_RE = re.compile(
    r"(?i)^<bash\s+output\s+unavailable:\s+output\s+file\s+.+?\s+could\s+not\s+be\s+read\s+\([^)]+\)\."
    r"\s+This\s+usually\s+means\s+another\s+.+?\s+process\s+in\s+the\s+same\s+project\s+deleted\s+it\s+during\s+startup\s+cleanup\.>$"
)
_TEAMMATE_MESSAGE_RE = re.compile(
    r"(?is)^\s*<\s*teammate_message\b[^>]*>\s*(.*?)\s*</\s*teammate_message\s*>\s*$"
)
_SYSTEM_REMINDER_RE = re.compile(
    r"(?is)^\s*<\s*system-reminder\b[^>]*>\s*(.*?)\s*</\s*system-reminder\s*>\s*$"
)
_TOKEN_USAGE_REMINDER_RE = re.compile(
    r"(?i)^token\s+usage\s*:\s*[\d,.]+\s*/\s*[\d,.]+\s*;\s*[\d,.]+\s+remaining\s*$"
)
_USD_BUDGET_REMINDER_RE = re.compile(
    r"(?i)^usd\s+budget\s*:\s*\$?-?[\d,.]+(?:\.\d+)?\s*/\s*\$?-?[\d,.]+(?:\.\d+)?\s*;\s*"
    r"\$?-?[\d,.]+(?:\.\d+)?\s+remaining\s*$"
)
_OUTPUT_TOKEN_USAGE_REMINDER_RE = re.compile(
    r"(?i)^output\s+tokens\s*(?:[-\u2014]|\u00e2\u20ac\u201d)\s*turn\s*:\s*[\d,.]+"
    r"(?:\s*/\s*[\d,.]+)?\s*(?:[.\u00b7]|\u00c2\u00b7)\s*session\s*:\s*[\d,.]+\s*$"
)
_AUTO_COMPACT_REMINDER_RE = re.compile(
    r"(?i)^auto-compact\s+is\s+enabled\.\s+when\s+the\s+context\s+window\s+is\s+nearly\s+full,"
)
_AGENT_LAUNCH_NOTICE_RE = re.compile(
    r"(?is)^\s*(?:remote\s+agent\s+launched\s+in\s+ccr|async\s+agent\s+launched\s+successfully)\b"
    r".*\bagent\s+is\s+(?:running\s+remotely|working\s+in\s+the\s+background)\b"
)
_PERMISSION_DELIVERY_NOTICE_RE = re.compile(
    r"(?i)^\s*(?:"
    r"notifications/[^:\s]+/channel/permission\s*:\s*\S+\s*(?:->|\?|→)\s*\w+\s*\([^)]+\)"
    r"|\[PermissionSync\]\s+Sent\s+(?:sandbox\s+)?permission\s+response\s+for\s+\S+.*\s+via\s+mailbox"
    r")\s*$"
)
_PERMISSION_STATE_NOTICE_RE = re.compile(
    r"(?i)^\s*\[PermissionSync\]\s+(?:"
    r"Resolved\s+request\s+\S+\s+with\s+\w+"
    r"|Pending\s+request\s+not\s+found\s*:\s*\S+"
    r"|Deleted\s+resolved\s+permission\s*:\s*\S+"
    r")\s*$"
)
_MEMORY_SAVED_AGE_HEADER_RE = re.compile(
    r"(?i)^(memory\s+\(saved\s+).+?(\)\s*:\s*.+:\s*)$"
)
_TASK_STOPPED_STATUS_RE = re.compile(
    r"(?i)^(Task\s+\"[^\"]+\"\s*)\([^)]+\)(\s+was\s+stopped\s+by\s+the\s+user\.)$"
)
_BACKGROUND_AGENT_RUNNING_RE = re.compile(
    r"(?i)^(Background\s+agent\s+\"[^\"]+\"\s*)\([^)]+\)(\s+is\s+still\s+running\.)"
)
_TASK_STATUS_RENDERED_ID_RE = re.compile(
    r"(?i)\bTask\s+[^()\s]+(\s+\(type:\s*)"
)
_TASK_STATUS_PARTIAL_OUTPUT_RE = re.compile(
    r"(?i)(read\s+partial\s+output\s+at\s+).+?(\s+or\s+send\s+it\s+a\s+message\b)"
)
_TASK_STATUS_OUTPUT_FILE_RE = re.compile(
    r"(?i)(Read\s+the\s+output\s+file\s+to\s+retrieve\s+the\s+result:\s*)\S+"
)
_USAGE_TAG_RE = re.compile(r"(?i)</?\s*usage\s*>")
_MATERIAL_URL_RE = re.compile(r"https?://[^\s<>)\]\"']+")


def normalize_material_repo_aliases(value: str) -> str:
    """Collapse known CHILI checkout aliases without changing material file identity."""
    return _CHILI_REPO_ROOT_ALIAS_RE.sub("<chili-repo>", str(value or "").replace("\\", "/"))


def normalize_material_output_notice(value: str) -> str:
    """Collapse volatile saved-output/truncation metadata while keeping the signal."""
    line = str(value or "").strip()
    if _TRUNCATED_FULL_OUTPUT_RE.match(line):
        return "[Truncated. Full output: <saved-output>]"
    if _OUTPUT_TRUNCATED_SAVED_RE.match(line):
        return "Output truncated. Full output saved to: <saved-output>"
    if _PERSISTED_OUTPUT_SAVED_RE.match(line):
        return "Output too large (<size>). Full output saved to: <saved-output>"
    if _PERSISTED_OUTPUT_PREVIEW_HEADER_RE.match(line):
        return "Preview (first <preview-size>):"
    if _TOOL_RESULT_CLEARED_RE.match(line):
        return ""
    if _PERMISSION_DELIVERY_NOTICE_RE.match(line):
        return ""
    if _PERMISSION_STATE_NOTICE_RE.match(line):
        return ""
    if _OUTPUT_TRUNCATED_DISK_CAP_RE.match(line):
        return "[output truncated: exceeded disk cap]"
    if _TASK_OUTPUT_READ_FAILED_RE.match(line):
        return "TaskOutput.#readStdoutFromFile: failed to read <saved-output>"
    if _BASH_OUTPUT_UNAVAILABLE_RE.match(line):
        return "<bash output unavailable: output file <saved-output> could not be read>"
    line = _OUTPUT_TRUNCATED_REMOVED_RE.sub("... [output truncated - <removed> removed]", line)
    line = _OUTPUT_TRUNCATED_EXCEEDED_CHARS_RE.sub(
        "... [output truncated - exceeded <characters> characters]",
        line,
    )
    line = _PYTEST_SUMMARY_TIMING_RE.sub(r"\1 in <duration>\2", line)
    line = _UNITTEST_SUMMARY_TIMING_RE.sub(r"\1 in <duration>\2", line)
    return _FLUTTER_TEST_ELAPSED_RE.sub(r"<elapsed> \1", line)


def normalize_material_protocol_envelope(value: str) -> str:
    """Collapse volatile mailbox XML wrappers while keeping their message body material."""
    text = str(value or "").strip()
    teammate_match = _TEAMMATE_MESSAGE_RE.match(text)
    if not teammate_match:
        return text
    body = stable_material_text(teammate_match.group(1))
    if not body:
        return "<teammate_message></teammate_message>"
    return f"<teammate_message>{body}</teammate_message>"


def normalize_material_runtime_reminder(value: str) -> str:
    """Drop rendered desktop runtime reminders that do not change task semantics."""
    text = str(value or "").strip()
    body = text
    wrapper_match = _SYSTEM_REMINDER_RE.match(text)
    if wrapper_match:
        body = wrapper_match.group(1).strip()
    compact_body = " ".join(body.split())
    if (
        _TOKEN_USAGE_REMINDER_RE.match(compact_body)
        or _USD_BUDGET_REMINDER_RE.match(compact_body)
        or _OUTPUT_TOKEN_USAGE_REMINDER_RE.match(compact_body)
        or _AUTO_COMPACT_REMINDER_RE.match(compact_body)
        or _AGENT_LAUNCH_NOTICE_RE.match(text)
        or _PERMISSION_DELIVERY_NOTICE_RE.match(text)
    ):
        return ""
    return text


def normalize_material_memory_header(value: str) -> str:
    """Collapse volatile rendered memory age while keeping memory identity."""
    return _MEMORY_SAVED_AGE_HEADER_RE.sub(r"\1<age>\2", str(value or ""))


def normalize_material_task_status(value: str) -> str:
    """Collapse rendered task-status transport ids while preserving status/delta."""
    line = str(value or "")
    line = _TASK_STOPPED_STATUS_RE.sub(r"\1(<task-id>)\2", line)
    line = _BACKGROUND_AGENT_RUNNING_RE.sub(r"\1(<task-id>)\2", line)
    line = _TASK_STATUS_RENDERED_ID_RE.sub(r"Task <task-id>\1", line)
    line = _TASK_STATUS_PARTIAL_OUTPUT_RE.sub(r"\1<saved-output>\2", line)
    return _TASK_STATUS_OUTPUT_FILE_RE.sub(r"\1<saved-output>", line)


def normalize_material_usage_trailer(value: str) -> str:
    """Drop rendered agent usage trailer lines that only carry replay telemetry."""
    line = _USAGE_TAG_RE.sub("", str(value or "")).strip()
    label_match = _LABEL_RE.match(line)
    if label_match and is_volatile_material_key(label_match.group(1)):
        return ""
    return line


def normalize_material_url_tracking(value: str) -> str:
    """Remove URL analytics params without collapsing meaningful resource identity."""
    def _clean_match(match: re.Match[str]) -> str:
        raw = match.group(0)
        trailing = ""
        while raw and raw[-1] in ".,;:":
            trailing = raw[-1] + trailing
            raw = raw[:-1]
        try:
            parts = urlsplit(raw)
        except Exception:
            return match.group(0)
        if not parts.scheme or not parts.netloc:
            return match.group(0)
        query = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key.lower() not in _TRACKING_QUERY_KEYS and not key.lower().startswith("utm_")
        ]
        cleaned = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), parts.fragment))
        return f"{cleaned}{trailing}"

    return _MATERIAL_URL_RE.sub(_clean_match, str(value or ""))


def material_key_name(value: object) -> str:
    text = str(value).strip()
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def is_volatile_material_key(value: object) -> bool:
    return material_key_name(value) in _VOLATILE_MATERIAL_KEYS


def _plain_id_is_volatile_for_object(value: dict[object, object]) -> bool:
    keys = {material_key_name(key) for key in value}
    return "id" in keys and {"tool_name", "tool_use_id", "team_name"}.issubset(keys) and bool(
        keys & _PLAIN_ID_VOLATILE_ENVELOPE_KEYS
    )


def _empty_message_wrapper_is_volatile(value: dict[str, object]) -> bool:
    keys = {material_key_name(key) for key in value}
    return bool(keys) and keys.issubset({"content", "role"}) and str(value.get("content") or "") == ""


def _stable_async_hook_response(value: dict[object, object]) -> object:
    """Mirror desktop rendering: only hook messages/additional context reach the model."""
    raw_response = value.get("response")
    if not isinstance(raw_response, dict):
        return _DROP_MATERIAL

    stable: dict[str, object] = {"type": "async_hook_response"}
    system_message = raw_response.get("systemMessage")
    if isinstance(system_message, str) and system_message.strip():
        stable["systemMessage"] = stable_material_text(system_message)

    hook_output = raw_response.get("hookSpecificOutput")
    if isinstance(hook_output, dict):
        additional_context = hook_output.get("additionalContext")
        if additional_context:
            stable["additionalContext"] = stable_material_value(additional_context)

    if len(stable) == 1:
        return _DROP_MATERIAL
    return stable


def _stable_hook_attachment(value: dict[object, object], protocol_type: str) -> object:
    hook_name = stable_material_text(str(value.get("hookName") or value.get("hook_name") or ""))
    if protocol_type == "hook_success":
        hook_event = str(value.get("hookEvent") or value.get("hook_event") or "")
        if hook_event not in {"SessionStart", "UserPromptSubmit"}:
            return _DROP_MATERIAL
        content = stable_material_text(str(value.get("content") or ""))
        if not content:
            return _DROP_MATERIAL
        return {"type": protocol_type, "hookName": hook_name, "content": content}

    if protocol_type == "hook_additional_context":
        content = value.get("content")
        stable_content = stable_material_value(content)
        if not stable_content:
            return _DROP_MATERIAL
        return {"type": protocol_type, "hookName": hook_name, "content": stable_content}

    if protocol_type == "hook_stopped_continuation":
        message = stable_material_text(str(value.get("message") or ""))
        if not message:
            return _DROP_MATERIAL
        return {"type": protocol_type, "hookName": hook_name, "message": message}

    if protocol_type == "hook_blocking_error":
        raw_error = value.get("blockingError") or value.get("blocking_error")
        if not isinstance(raw_error, dict):
            return _DROP_MATERIAL
        command = stable_material_text(str(raw_error.get("command") or ""))
        blocking_error = stable_material_text(str(raw_error.get("blockingError") or raw_error.get("blocking_error") or ""))
        if not command and not blocking_error:
            return _DROP_MATERIAL
        return {
            "type": protocol_type,
            "hookName": hook_name,
            "blockingError": {
                "command": command,
                "blockingError": blocking_error,
            },
        }

    return _DROP_MATERIAL


def _stable_skill_discovery(value: dict[object, object]) -> object:
    skills = value.get("skills")
    if not isinstance(skills, (list, tuple)) or not skills:
        return _DROP_MATERIAL

    stable_skills: list[dict[str, object]] = []
    for skill in skills:
        if not isinstance(skill, dict):
            continue
        stable_skill: dict[str, object] = {}
        name = stable_material_text(str(skill.get("name") or ""))
        description = stable_material_text(str(skill.get("description") or ""))
        if name:
            stable_skill["name"] = name
        if description:
            stable_skill["description"] = description
        if stable_skill:
            stable_skills.append(stable_skill)

    if not stable_skills:
        return _DROP_MATERIAL
    return {"type": "skill_discovery", "skills": stable_skills}


def _normalize_transcript_volatile_prefix(line: str) -> str:
    role_match = _TRANSCRIPT_ROLE_RE.match(line)
    if not role_match:
        return line
    body = role_match.group(1).strip()
    prefix_match = _VOLATILE_PREFIX_RE.match(body)
    if prefix_match and is_volatile_material_key(prefix_match.group(1)):
        body = body[prefix_match.end() :].strip()
    label_match = _LABEL_RE.match(body)
    if label_match and is_volatile_material_key(label_match.group(1)):
        return ""
    return body


def _normalize_inline_json_material(text: str) -> str:
    source = str(text or "")
    decoder = json.JSONDecoder()
    chunks: list[str] = []
    cursor = 0
    while cursor < len(source):
        marker_positions = [
            pos
            for pos in (source.find("{", cursor), source.find("[", cursor))
            if pos >= 0
        ]
        if not marker_positions:
            chunks.append(source[cursor:])
            break
        start = min(marker_positions)
        chunks.append(source[cursor:start])
        try:
            parsed, end = decoder.raw_decode(source[start:])
        except ValueError:
            chunks.append(source[start : start + 1])
            cursor = start + 1
            continue
        stable = stable_material_text(parsed)
        if stable and stable not in ("{}", "[]"):
            chunks.append(stable)
        cursor = start + end
    return "".join(chunks).strip()


def stable_material_text(value: object) -> str:
    if isinstance(value, (dict, list, tuple)):
        stable_value = stable_material_value(value)
        if stable_value is _DROP_MATERIAL:
            return ""
        return json.dumps(
            stable_value,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        parsed = None
    if parsed is not None:
        stable_parsed = stable_material_value(parsed)
        if stable_parsed is _DROP_MATERIAL:
            return ""
        return json.dumps(
            stable_parsed,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    value = normalize_material_protocol_envelope(value)
    value = normalize_material_runtime_reminder(value)
    if not value:
        return ""
    lines: list[str] = []
    for raw_line in normalize_material_repo_aliases(value).splitlines():
        line = normalize_material_output_notice(" ".join(raw_line.strip().split()))
        line = normalize_material_memory_header(line)
        line = normalize_material_task_status(line)
        line = normalize_material_usage_trailer(line)
        line = normalize_material_url_tracking(line)
        line = _normalize_transcript_volatile_prefix(line)
        if not line:
            continue
        match = _LABEL_RE.match(line)
        if match and is_volatile_material_key(match.group(1)):
            continue
        xml_match = _XML_TAG_RE.match(line)
        if xml_match and is_volatile_material_key(xml_match.group(1)):
            continue
        line = _XML_LEAF_TAG_RE.sub(
            lambda tag_match: "" if is_volatile_material_key(tag_match.group(1)) else tag_match.group(0),
            line,
        )
        line = _XML_ATTR_RE.sub(
            lambda attr_match: "" if is_volatile_material_key(attr_match.group(1)) else attr_match.group(0),
            line,
        )
        line = _normalize_inline_json_material(line)
        if not line.strip():
            continue
        lines.append(line)
    return "\n".join(lines)


def stable_material_value(value: object) -> object:
    if isinstance(value, dict):
        stable: dict[str, object] = {}
        drop_plain_id = _plain_id_is_volatile_for_object(value)
        raw_type = value.get("type")
        protocol_type = str(raw_type).strip() if isinstance(raw_type, str) else ""
        if protocol_type == "async_hook_response":
            return _stable_async_hook_response(value)
        if protocol_type in _HOOK_TYPES_WITH_VISIBLE_MATERIAL:
            return _stable_hook_attachment(value, protocol_type)
        if protocol_type == "skill_discovery":
            return _stable_skill_discovery(value)
        if protocol_type in _TELEMETRY_ONLY_MATERIAL_TYPES:
            return _DROP_MATERIAL
        protocol_volatile_keys = _PROTOCOL_MESSAGE_VOLATILE_KEYS_BY_TYPE.get(protocol_type, set())
        for key in sorted(value, key=lambda item: str(item)):
            key_name = material_key_name(key)
            if key_name == "id" and drop_plain_id:
                continue
            if key_name in protocol_volatile_keys:
                continue
            if key_name in _VOLATILE_MATERIAL_KEYS:
                continue
            stable_value = stable_material_value(value[key])
            if stable_value is _DROP_MATERIAL:
                continue
            stable[str(key)] = stable_value
        if _empty_message_wrapper_is_volatile(stable):
            return _DROP_MATERIAL
        return stable
    if isinstance(value, (list, tuple)):
        stable_items = []
        for item in value:
            stable_item = stable_material_value(item)
            if stable_item is _DROP_MATERIAL:
                continue
            stable_items.append(stable_item)
        return stable_items
    if isinstance(value, str):
        return stable_material_text(value)
    return value


def stable_material_fingerprint(*, schema: str, payload: object) -> str:
    material = stable_material_value(payload)
    if material is _DROP_MATERIAL:
        material = None
    stable_payload = {
        "schema": schema,
        "material": material,
    }
    encoded = json.dumps(stable_payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
