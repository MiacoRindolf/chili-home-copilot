from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.autopilot_frontier_bakeoff_benchmark import _command_text, _escape_cell  # noqa: E402
from scripts.autopilot_local_model_evidence_recorder import (  # noqa: E402
    DEFAULT_SOURCE_DIR,
    LocalModelEvidenceRecorderError,
    record_local_model_evidence,
)
from scripts.autopilot_model_candidate_artifact_builder import (  # noqa: E402
    MODEL_CANDIDATE_DROP_PROMPT_PACK_SCHEMA_VERSION,
    MODEL_CANDIDATE_DROP_SCHEMA_VERSION,
    source_specific_prompt_contract,
    validate_prompt_pack_markdown,
)
from scripts.autopilot_real_chili_candidate_bakeoff import default_cases  # noqa: E402


DEFAULT_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "LOCAL_MODEL_CANDIDATE_RUN.md"
DEFAULT_WORK_DIR = REPO_ROOT / "project_ws" / "AgentOps" / "local_model_candidate_runs"
LOCAL_MODEL_CANDIDATE_RUNNER_SCHEMA_VERSION = "chili.local-model-candidate-runner.v1"
LOCAL_MODEL_SUITE_DIAGNOSTICS_SCHEMA_VERSION = "chili.local-model-suite-diagnostics.v1"
DEFAULT_MODEL_NAME = "qwen3:4b"
DEFAULT_CASE_ID = "real-chili-preflight-candidate-wins"
SOURCE_KIND = "local_model"


class LocalModelCandidateRunnerError(ValueError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_name(value: object, *, fallback: str) -> str:
    raw = str(value or fallback).strip().lower()
    safe = re.sub(r"[^a-z0-9._-]+", "-", raw).strip(".-")
    return safe or fallback


def _required_text(payload: Mapping[str, object], key: str, *, label: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise LocalModelCandidateRunnerError(f"{label}.{key} is required")
    return value.strip()


def _strip_terminal_control(text: str) -> str:
    lines: list[str] = []
    line: list[str] = []
    cursor = 0
    index = 0
    while index < len(text):
        character = text[index]
        if character == "\x1b" and index + 1 < len(text) and text[index + 1] == "[":
            match = re.match(r"\x1b\[([0-9;?]*)([ -/]*)([@-~])", text[index:])
            if match:
                raw_count, _private, final = match.groups()
                first_count = raw_count.split(";", 1)[0] if raw_count else ""
                count = int(first_count) if first_count.isdigit() else 1
                if final == "D":
                    cursor = max(0, cursor - count)
                elif final == "C":
                    cursor += count
                    if cursor > len(line):
                        line.extend(" " for _ in range(cursor - len(line)))
                elif final == "G":
                    cursor = max(0, count - 1)
                    if cursor > len(line):
                        line.extend(" " for _ in range(cursor - len(line)))
                elif final == "K":
                    del line[cursor:]
                index += len(match.group(0))
                continue
        if character == "\r":
            cursor = 0
            index += 1
            continue
        if character == "\n":
            lines.append("".join(line))
            line = []
            cursor = 0
            index += 1
            continue
        if character == "\b":
            cursor = max(0, cursor - 1)
            index += 1
            continue
        if character in "\t" or ord(character) >= 32:
            if cursor < len(line):
                line[cursor] = character
            else:
                if cursor > len(line):
                    line.extend(" " for _ in range(cursor - len(line)))
                line.append(character)
            cursor += 1
        index += 1
    lines.append("".join(line))
    normalized = "\n".join(lines)
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", normalized)


def _process_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _timeout_partial_output(exc: subprocess.TimeoutExpired) -> str:
    output = _process_text(getattr(exc, "stdout", None) or getattr(exc, "output", None))
    error = _process_text(getattr(exc, "stderr", None))
    return "\n".join(part for part in (output, error) if part.strip()).strip()


def _case_by_id(case_id: str):
    for case in default_cases():
        if case.case_id == case_id:
            return case
    known = ", ".join(case.case_id for case in default_cases())
    raise LocalModelCandidateRunnerError(f"unknown case_id {case_id}; expected one of: {known}")


def render_compact_prompt_pack(
    *,
    case_id: str = DEFAULT_CASE_ID,
    model_name: str = DEFAULT_MODEL_NAME,
    generated_at: datetime | None = None,
) -> str:
    case = _case_by_id(case_id)
    generated_at = generated_at or datetime.now(timezone.utc)
    command = _command_text(case.test_command)
    planned_file = case.incumbent.planned_file
    drop_template = {
        "schema": MODEL_CANDIDATE_DROP_SCHEMA_VERSION,
        "case_id": case.case_id,
        "candidate_id": f"{SOURCE_KIND}-{case.case_id}",
        "model_name": model_name,
        "source_kind": SOURCE_KIND,
        "patch": "<unified diff for the planned file only>",
        "planned_file": planned_file,
        "expected_changed_files": [planned_file],
        "declared_commands": [command],
        "duration_seconds": 0.0,
        "cost_units": 0.0,
        "notes": "<short explanation>",
    }
    lines = [
        "# CHILI Compact Local Model Candidate Prompt Pack",
        "",
        f"- Schema: {MODEL_CANDIDATE_DROP_PROMPT_PACK_SCHEMA_VERSION}",
        f"- Drop schema: {MODEL_CANDIDATE_DROP_SCHEMA_VERSION}",
        f"- Generated UTC: {generated_at.isoformat().replace('+00:00', 'Z')}",
        f"- Source kind: {SOURCE_KIND}",
        f"- Model name: {model_name}",
        "- Cases: 1",
        "- Required behavior: return exactly one JSON object with a unified-diff patch for the planned file.",
        "- Safety: work only from the temporary fixture text below; do not touch the real checkout, git state, runtime, database, broker, deployment, credentials, or live-trading controls.",
        "",
        "## Output Contract",
        "",
        "- Return only valid JSON. Do not wrap the answer in Markdown.",
        "- Replace every placeholder in the template; do not copy the template as your final answer.",
        "- The `patch` value must start with `diff --git`.",
        "- Put the unified diff in the `patch` string. Do not include unrelated files.",
        "- Include the listed behavior command exactly in `declared_commands`.",
        "- If you are uncertain, still return your best scoped patch; CHILI will reject unsafe or failing candidates.",
        "",
        "## Source-Specific Operating Contract",
        "",
    ]
    lines.extend(f"- {item}" for item in source_specific_prompt_contract(SOURCE_KIND, model_name))
    lines.extend(
        [
            "",
            f"## Case: {case.case_id}",
            "",
            f"- Comparison class: {case.bakeoff_class}",
            f"- Planned file: {planned_file}",
            f"- Expected changed files: {planned_file}",
            f"- Required behavior command: `{command}`",
            "",
            "### Fixture Files",
            "",
        ]
    )
    for path, content in sorted(case.files.items()):
        lines.extend(
            [
                f"#### `{path}`",
                "",
                "```text",
                content.rstrip(),
                "```",
                "",
            ]
        )
    lines.extend(
        [
            "### JSON Response Template",
            "",
            "```json",
            json.dumps(drop_template, indent=2, sort_keys=True),
            "```",
            "",
        ]
    )
    markdown = "\n".join(lines)
    validate_prompt_pack_markdown(
        markdown,
        source_kind=SOURCE_KIND,
        model_name=model_name,
        label="compact_prompt_pack",
    )
    return markdown


def render_compact_suite_prompt_pack(
    *,
    model_name: str = DEFAULT_MODEL_NAME,
    generated_at: datetime | None = None,
) -> str:
    cases = default_cases()
    generated_at = generated_at or datetime.now(timezone.utc)
    lines = [
        "# CHILI Compact Local Model Candidate Suite Prompt Pack",
        "",
        f"- Schema: {MODEL_CANDIDATE_DROP_PROMPT_PACK_SCHEMA_VERSION}",
        f"- Drop schema: {MODEL_CANDIDATE_DROP_SCHEMA_VERSION}",
        f"- Generated UTC: {generated_at.isoformat().replace('+00:00', 'Z')}",
        f"- Source kind: {SOURCE_KIND}",
        f"- Model name: {model_name}",
        f"- Cases: {len(cases)}",
        "- Required behavior: return one valid JSON object per case, each with an inline unified-diff `patch` string for the planned file.",
        "- Safety: work only from the temporary fixture text below; do not touch the real checkout, git state, runtime, database, broker, deployment, credentials, or live-trading controls.",
        "",
        "## Output Contract",
        "",
        "- Return only JSON objects. Do not wrap the answer in Markdown, prose, or a JSON array.",
        "- Return exactly one JSON object for each case section below.",
        "- Use the listed `case_id`, `planned_file`, `expected_changed_files`, and `declared_commands` exactly.",
        "- The `patch` value must start with `diff --git` and must be inline in the JSON object.",
        "- Keep each patch scoped to the planned file. Do not include unrelated files.",
        "- Do not emit `patch_file` or `provenance`; CHILI records provenance after parsing your response.",
        "- If a candidate is intentionally unsafe for a regression case, still return the smallest patch you believe best satisfies the tests; CHILI will replay and reject unsafe or failing candidates.",
        "",
        "## Source-Specific Operating Contract",
        "",
    ]
    lines.extend(f"- {item}" for item in source_specific_prompt_contract(SOURCE_KIND, model_name))
    lines.append("")
    for case in cases:
        command = _command_text(case.test_command)
        planned_file = case.incumbent.planned_file
        drop_template = {
            "schema": MODEL_CANDIDATE_DROP_SCHEMA_VERSION,
            "case_id": case.case_id,
            "candidate_id": f"{SOURCE_KIND}-{case.case_id}",
            "model_name": model_name,
            "source_kind": SOURCE_KIND,
            "patch": "<unified diff for the planned file only>",
            "planned_file": planned_file,
            "expected_changed_files": [planned_file],
            "declared_commands": [command],
            "duration_seconds": 0.0,
            "cost_units": 0.0,
            "notes": "<short explanation>",
        }
        lines.extend(
            [
                f"## Case: {case.case_id}",
                "",
                f"- Comparison class: {case.bakeoff_class}",
                f"- Planned file: {planned_file}",
                f"- Expected changed files: {planned_file}",
                f"- Required behavior command: `{command}`",
                "",
                "### Fixture Files",
                "",
            ]
        )
        for path, content in sorted(case.files.items()):
            lines.extend(
                [
                    f"#### `{path}`",
                    "",
                    "```text",
                    content.rstrip(),
                    "```",
                    "",
                ]
            )
        lines.extend(
            [
                "### JSON Response Template",
                "",
                "```json",
                json.dumps(drop_template, indent=2, sort_keys=True),
                "```",
                "",
            ]
        )
    markdown = "\n".join(lines)
    validate_prompt_pack_markdown(
        markdown,
        source_kind=SOURCE_KIND,
        model_name=model_name,
        label="compact_suite_prompt_pack",
    )
    return markdown


def _json_objects_from_balanced_text(text: str) -> list[Mapping[str, object]]:
    objects: list[Mapping[str, object]] = []
    for start, character in enumerate(text):
        if character != "{":
            continue
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            current = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    in_string = False
                continue
            if current == '"':
                in_string = True
            elif current == "{":
                depth += 1
            elif current == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : index + 1]
                    try:
                        parsed = json.loads(candidate)
                    except json.JSONDecodeError:
                        break
                    if not isinstance(parsed, Mapping):
                        break
                    objects.append(parsed)
                    break
    return objects


def parse_model_response(response_text: str) -> Mapping[str, object]:
    response_text = _strip_terminal_control(response_text)
    objects: list[Mapping[str, object]] = []
    for fenced in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", response_text, flags=re.DOTALL | re.I):
        try:
            parsed = json.loads(fenced.group(1))
        except json.JSONDecodeError as exc:
            raise LocalModelCandidateRunnerError(f"model response JSON block is invalid: {exc}") from exc
        if not isinstance(parsed, Mapping):
            raise LocalModelCandidateRunnerError("model response JSON block must be an object")
        objects.append(parsed)
    objects.extend(_json_objects_from_balanced_text(response_text))
    for payload in objects:
        patch = payload.get("patch")
        if isinstance(patch, str) and "diff --git" in patch:
            cleaned_payload = dict(payload)
            cleaned_payload["patch"] = _decode_model_patch_text(patch)
            return cleaned_payload
    fallback_patch = _fallback_patch_from_response(response_text)
    if fallback_patch:
        return {
            "candidate_id": f"{SOURCE_KIND}-extracted-candidate",
            "notes": "Patch extracted from non-JSON local-model response.",
            "patch": fallback_patch,
        }
    if objects:
        return objects[0]
    raise LocalModelCandidateRunnerError("model response did not contain a valid JSON object")


def parse_model_response_suite(response_text: str, *, case_ids: Sequence[str]) -> list[Mapping[str, object]]:
    response_text = _strip_terminal_control(response_text)
    expected = set(case_ids)
    by_case: dict[str, Mapping[str, object]] = {}
    for payload in _json_objects_from_balanced_text(response_text):
        raw_case_id = payload.get("case_id")
        patch = payload.get("patch")
        if not isinstance(raw_case_id, str) or raw_case_id not in expected:
            continue
        if not isinstance(patch, str) or "diff --git" not in patch:
            continue
        cleaned_payload = dict(payload)
        cleaned_payload["patch"] = _decode_model_patch_text(patch)
        by_case[raw_case_id] = cleaned_payload
    missing = [case_id for case_id in case_ids if case_id not in by_case]
    if missing:
        raise LocalModelCandidateRunnerError(
            "model response did not contain valid candidate JSON for cases: " + ", ".join(missing)
        )
    return [by_case[case_id] for case_id in case_ids]


def _fallback_patch_from_response(response_text: str) -> str:
    patch_matches = list(
        re.finditer(
            r'"patch"\s*:\s*"(?P<patch>diff --git.*?)(?:"\s*(?:,|\}))',
            response_text,
            flags=re.DOTALL,
        )
    )
    for match in reversed(patch_matches):
        raw_patch = match.group("patch")
        patch = _decode_model_patch_text(raw_patch)
        if "diff --git" in patch:
            return patch

    lines = response_text.splitlines()
    start_index = -1
    for index, line in enumerate(lines):
        if "diff --git " in line:
            start_index = index
    if start_index < 0:
        return ""
    collected: list[str] = []
    for line in lines[start_index:]:
        stripped = line.strip()
        if not stripped and collected:
            break
        if stripped.startswith(("But ", "Given ", "So ", "Final ", "Note:")) and collected:
            break
        collected.append(line)
    return _decode_model_patch_text("\n".join(collected))


def _decode_model_patch_text(raw_patch: str) -> str:
    patch = _decode_jsonish_patch_escapes(raw_patch)
    patch = _strip_terminal_control(patch)
    patch = _repair_wrapped_patch_lines(patch)
    patch = _recount_unified_diff_hunks(patch)
    lines = patch.splitlines()
    if lines and "diff --git" in lines[0]:
        return "\n".join(lines).strip() + "\n"
    trimmed_lines = [line.lstrip() for line in lines]
    for index, line in enumerate(trimmed_lines):
        if line.startswith("diff --git "):
            return "\n".join(trimmed_lines[index:]).strip() + "\n"
    return patch.strip() + ("\n" if patch.strip() else "")


def _decode_jsonish_patch_escapes(raw_patch: str) -> str:
    patch = raw_patch
    for escaped_newline in ("\\\\r\\\\n", "\\\\n", "\\r\\n", "\\n"):
        patch = patch.replace(escaped_newline, "\n")
    patch = patch.replace("\\\\t", "\t").replace("\\t", "\t")
    patch = patch.replace('\\"', '"').replace("\\\\", "\\")
    return patch


def _starts_new_patch_line(line: str) -> bool:
    if line.startswith(
        (
            "diff --git ",
            "index ",
            "--- ",
            "+++ ",
            "@@ ",
            "new file mode ",
            "deleted file mode ",
            "rename ",
            "similarity index ",
        )
    ):
        return True
    return line.startswith((" ", "+", "-", "\\"))


def _join_wrapped_patch_line(previous: str, current: str) -> bool:
    if previous.rstrip() == "index":
        return True
    if previous.startswith("index ") and not re.match(r"^index \S+\.\.\S+", previous.rstrip()):
        return True
    if previous.startswith("@@") and not re.search(r" @@(?: |$)", previous):
        return True
    if current.startswith("->") and previous.startswith((" ", "+", "-")) and previous.rstrip().endswith(")"):
        return True
    return not _starts_new_patch_line(current)


def _repair_wrapped_patch_lines(patch: str) -> str:
    repaired: list[str] = []
    for line in patch.splitlines():
        if not repaired:
            repaired.append(line)
            continue
        if _join_wrapped_patch_line(repaired[-1], line):
            repaired[-1] += line
        else:
            repaired.append(line)
    return "\n".join(repaired)


def _recount_unified_diff_hunks(patch: str) -> str:
    lines = patch.splitlines()
    output: list[str] = []
    index = 0
    header_re = re.compile(r"^@@ -(?P<old_start>\d+)(?:,\d+)? \+(?P<new_start>\d+)(?:,\d+)? @@(?P<section>.*)$")
    while index < len(lines):
        line = lines[index]
        match = header_re.match(line)
        if not match:
            output.append(line)
            index += 1
            continue

        hunk_lines: list[str] = []
        index += 1
        while index < len(lines):
            candidate = lines[index]
            if candidate.startswith(("diff --git ", "@@ ")):
                break
            hunk_lines.append(candidate)
            index += 1

        old_count = sum(1 for candidate in hunk_lines if not candidate.startswith(("+", "\\")))
        new_count = sum(1 for candidate in hunk_lines if not candidate.startswith(("-", "\\")))
        section = match.group("section") or ""
        output.append(
            f"@@ -{match.group('old_start')},{old_count} +{match.group('new_start')},{new_count} @@{section}"
        )
        output.extend(hunk_lines)
    return "\n".join(output)


def _patch_from_payload(payload: Mapping[str, object]) -> str:
    patch = _required_text(payload, "patch", label="model_response")
    patch = _decode_model_patch_text(patch.strip())
    if patch.startswith("```"):
        patch = re.sub(r"^```(?:diff|patch)?\s*", "", patch, flags=re.I).strip()
        patch = re.sub(r"\s*```$", "", patch).strip()
    if "diff --git" not in patch:
        raise LocalModelCandidateRunnerError("model_response.patch must contain a unified diff")
    return patch + ("\n" if not patch.endswith("\n") else "")


def _write_candidate_drop(
    *,
    raw_dir: Path,
    payload: Mapping[str, object],
    case_id: str,
    model_name: str,
    duration_seconds: float,
) -> tuple[Path, Path]:
    case = _case_by_id(case_id)
    raw_dir.mkdir(parents=True, exist_ok=True)
    safe_case = _safe_name(case.case_id, fallback="case")
    patch_path = raw_dir / f"{safe_case}.patch"
    patch_path.write_text(_patch_from_payload(payload), encoding="utf-8")
    candidate_id = str(payload.get("candidate_id") or f"{SOURCE_KIND}-{safe_case}").strip()
    drop = {
        "schema": MODEL_CANDIDATE_DROP_SCHEMA_VERSION,
        "case_id": case.case_id,
        "candidate_id": candidate_id or f"{SOURCE_KIND}-{safe_case}",
        "model_name": model_name,
        "source_kind": SOURCE_KIND,
        "collected_at": _utc_now(),
        "patch_file": patch_path.name,
        "planned_file": case.incumbent.planned_file,
        "expected_changed_files": [case.incumbent.planned_file],
        "declared_commands": [_command_text(case.test_command)],
        "duration_seconds": float(duration_seconds),
        "cost_units": 0.0,
        "notes": str(payload.get("notes") or payload.get("explanation") or "").strip(),
    }
    drop_path = raw_dir / f"{safe_case}.json"
    drop_path.write_text(json.dumps(drop, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return drop_path, patch_path


def _run_ollama(
    *,
    model_name: str,
    prompt: str,
    timeout_seconds: int,
) -> tuple[str, float, str]:
    command = ("ollama", "run", model_name)
    started = time.monotonic()
    env = dict(os.environ)
    env.setdefault("NO_COLOR", "1")
    env["TERM"] = "dumb"
    try:
        result = subprocess.run(
            list(command),
            input=prompt,
            text=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        partial_output = _strip_terminal_control(_timeout_partial_output(exc))
        if partial_output.strip():
            return (
                partial_output,
                float(timeout_seconds),
                " ".join(command)
                + (
                    " < compact_prompt_pack.md "
                    f"(timed out after {timeout_seconds}s; partial response captured)"
                ),
            )
        raise LocalModelCandidateRunnerError(
            f"local model timed out after {timeout_seconds}s"
        ) from exc
    duration = time.monotonic() - started
    if result.returncode != 0:
        output = (result.stderr or result.stdout or "").strip()
        raise LocalModelCandidateRunnerError(
            f"local model command failed with exit {result.returncode}: {output[:500]}"
        )
    return result.stdout, duration, " ".join(command) + " < compact_prompt_pack.md"


def _suite_retry_context_from_diagnostics(diagnostics_path: Path) -> dict[str, str]:
    if not diagnostics_path.is_file():
        raise LocalModelCandidateRunnerError(
            f"suite diagnostics file does not exist: {diagnostics_path}"
        )
    try:
        diagnostics = json.loads(
            diagnostics_path.read_text(encoding="utf-8", errors="replace")
        )
    except json.JSONDecodeError as exc:
        raise LocalModelCandidateRunnerError(
            f"suite diagnostics JSON is invalid: {exc}"
        ) from exc
    if not isinstance(diagnostics, Mapping):
        raise LocalModelCandidateRunnerError("suite diagnostics must be a JSON object")
    schema = str(diagnostics.get("schema") or "").strip()
    if schema and schema != LOCAL_MODEL_SUITE_DIAGNOSTICS_SCHEMA_VERSION:
        raise LocalModelCandidateRunnerError(
            "suite diagnostics schema mismatch: "
            f"expected {LOCAL_MODEL_SUITE_DIAGNOSTICS_SCHEMA_VERSION}, got {schema}"
        )
    failed_case_id = str(diagnostics.get("failed_case_id") or "").strip()
    if not failed_case_id:
        for result in diagnostics.get("case_results") or []:
            if not isinstance(result, Mapping):
                continue
            status = str(result.get("status") or "")
            if status in {"model_failed", "parse_failed", "record_failed"}:
                failed_case_id = str(result.get("case_id") or "").strip()
                break
    if not failed_case_id:
        raise LocalModelCandidateRunnerError(
            "suite diagnostics do not identify a failed case"
        )
    _case_by_id(failed_case_id)
    return {
        "case_id": failed_case_id,
        "model_name": str(diagnostics.get("model_name") or DEFAULT_MODEL_NAME).strip()
        or DEFAULT_MODEL_NAME,
        "run_id": str(diagnostics.get("run_id") or "").strip(),
        "source_command": str(diagnostics.get("source_command") or "").strip(),
        "diagnostics": str(diagnostics_path),
    }


def _local_model_suite_recovery_routes(
    *,
    model_name: str,
    case_results: Sequence[Mapping[str, object]],
    failure_stage: str,
    failure_reason: str,
    timeout_seconds: int,
    diagnostics_path: Path | None = None,
) -> list[dict[str, object]]:
    failed_result: Mapping[str, object] = {}
    for result in case_results:
        status = str(result.get("status") or "")
        if status in {"model_failed", "parse_failed", "record_failed"}:
            failed_result = result
            break
    failed_case_id = str(failed_result.get("case_id") or "").strip()
    if not failed_case_id:
        return []

    safe_case = _safe_name(failed_case_id, fallback="case")
    retry_timeout = max(300, int(timeout_seconds or 0) * 2)
    diagnostics_text = str(diagnostics_path or "").strip()
    retry_command = (
        "python scripts/autopilot_local_model_candidate_runner.py "
        f"--retry-from-diagnostics {diagnostics_text} "
        f"--timeout-seconds {retry_timeout} --json"
        if diagnostics_text
        else (
            "python scripts/autopilot_local_model_candidate_runner.py "
            f"--case-id {failed_case_id} --model-name {model_name} "
            f"--timeout-seconds {retry_timeout} --json"
        )
    )
    import_response_command = (
        "python scripts/autopilot_local_model_candidate_runner.py "
        f"--retry-from-diagnostics {diagnostics_text} "
        f"--response-file <local-model-{safe_case}-response.txt> "
        "--run-id <real-local-run-id> "
        "--source-command <exact-local-model-command> --json"
        if diagnostics_text
        else (
            "python scripts/autopilot_local_model_candidate_runner.py "
            f"--case-id {failed_case_id} --model-name {model_name} "
            f"--response-file <local-model-{safe_case}-response.txt> "
            "--run-id <real-local-run-id> "
            "--source-command <exact-local-model-command> --json"
        )
    )
    lower_reason = failure_reason.lower()
    if failure_stage == "model" and "timed out" in lower_reason:
        action_label = "Retry failed case with longer timeout"
        reason = "The local model timed out before producing a parseable candidate."
    elif failure_stage == "parse":
        action_label = "Import corrected failed-case response"
        reason = "The local model produced output, but CHILI could not parse a valid candidate JSON/diff."
    elif failure_stage == "record":
        action_label = "Inspect and re-import generated local-model drops"
        reason = "The candidate parsed, but provenance recording rejected the generated drop."
    else:
        action_label = "Retry failed local-model case"
        reason = "The local-model suite stopped before all cases produced verified candidates."

    return [
        {
            "status": "available",
            "case_id": failed_case_id,
            "action_label": action_label,
            "reason": reason,
            "retry_command": retry_command,
            "import_response_command": import_response_command,
            "all_cases_retry_command": (
                "python scripts/autopilot_local_model_candidate_runner.py "
                f"--all-cases --model-name {model_name} "
                f"--timeout-seconds {retry_timeout} --json"
            ),
            "prompt_path": str(failed_result.get("prompt") or ""),
            "response_path": str(failed_result.get("response") or ""),
            "permission_boundary": (
                "local model diagnostics and evidence import only; no source/test edits, "
                "git/PR action, runtime restart, deployment, database migration, broker call, "
                "or live trading"
            ),
        }
    ]


def _suite_failure_summary(
    *,
    run_dir: Path,
    work_dir: Path,
    source_dir: Path,
    clean_run_id: str,
    model_name: str,
    command: str,
    case_ids: Sequence[str],
    case_results: Sequence[Mapping[str, object]],
    failure_stage: str,
    failure_reason: str,
    duration_seconds: float,
    write: bool,
    timeout_seconds: int = 300,
    prompt_path: Path | None = None,
    response_path: Path | None = None,
) -> dict[str, object]:
    attempted = [
        item
        for item in case_results
        if str(item.get("status") or "") not in {"", "pending"}
    ]
    parsed = [
        item
        for item in case_results
        if str(item.get("status") or "") in {"parsed", "recorded"}
    ]
    failed_case_id = ""
    for item in case_results:
        status = str(item.get("status") or "")
        if status in {"model_failed", "parse_failed", "record_failed"}:
            failed_case_id = str(item.get("case_id") or "")
            break

    diagnostics_path = run_dir / "suite_diagnostics.json"
    recovery_routes = _local_model_suite_recovery_routes(
        model_name=model_name,
        case_results=case_results,
        failure_stage=failure_stage,
        failure_reason=failure_reason,
        timeout_seconds=timeout_seconds,
        diagnostics_path=diagnostics_path if write else None,
    )
    diagnostics = {
        "schema": LOCAL_MODEL_SUITE_DIAGNOSTICS_SCHEMA_VERSION,
        "generated_utc": _utc_now(),
        "status": "failed",
        "run_id": clean_run_id,
        "model_name": model_name,
        "source_command": command,
        "failure_stage": failure_stage,
        "failure_reason": failure_reason,
        "case_ids": list(case_ids),
        "attempted_cases": len(attempted),
        "parsed_cases": len(parsed),
        "failed_case_id": failed_case_id,
        "duration_seconds": float(duration_seconds),
        "case_results": [dict(item) for item in case_results],
        "recovery_routes": recovery_routes,
        "permission_boundary": (
            "local model suite diagnostics only; no source/test edits, git/PR action, "
            "runtime restart, deployment, database migration, broker call, or live trading"
        ),
    }
    if write:
        diagnostics_path.write_text(
            json.dumps(diagnostics, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    return {
        "schema": LOCAL_MODEL_CANDIDATE_RUNNER_SCHEMA_VERSION,
        "generated_utc": _utc_now(),
        "status": "failed",
        "write": bool(write),
        "case_id": "all",
        "case_ids": list(case_ids),
        "cases": len(case_ids),
        "attempted_cases": len(attempted),
        "parsed_cases": len(parsed),
        "failed_case_id": failed_case_id,
        "failure_stage": failure_stage,
        "failure_reason": failure_reason,
        "model_name": model_name,
        "run_id": clean_run_id,
        "source_command": command,
        "run_dir": str(work_dir / _safe_name(clean_run_id, fallback="local-suite-run")),
        "prompt_pack": str(source_dir / "prompt_pack.md"),
        "compact_prompt_pack": "",
        "full_prompt_pack": str(prompt_path or run_dir / "full_prompt_pack.md"),
        "response": str(response_path or run_dir / "model_response.txt"),
        "diagnostics": str(diagnostics_path if write else source_dir / "suite_diagnostics.json"),
        "case_results": [dict(item) for item in case_results],
        "recovery_routes": recovery_routes,
        "ready_source_count_delta": 0,
        "promotion_ready": False,
        "next_action": (
            "Use the recovery route in suite_diagnostics.json to retry or import the failed "
            "local-model case, then rerun --all-cases after the failed case parses; otherwise "
            "import a stronger Codex/Claude all-cases response before publishing promotion scorecards."
        ),
        "permission_boundary": (
            "local model suite diagnostics only; no source/test edits, git/PR action, "
            "runtime restart, deployment, database migration, broker call, or live trading"
        ),
    }


def run_local_model_candidate_case(
    *,
    source_dir: Path = DEFAULT_SOURCE_DIR,
    work_dir: Path = DEFAULT_WORK_DIR,
    case_id: str = DEFAULT_CASE_ID,
    model_name: str = DEFAULT_MODEL_NAME,
    response_file: Path | None = None,
    run_id: str | None = None,
    source_command: str | None = None,
    write: bool = True,
    overwrite: bool = False,
    timeout_seconds: int = 300,
) -> dict[str, object]:
    case = _case_by_id(case_id)
    clean_run_id = run_id or f"local-{_safe_name(case.case_id, fallback='case')}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    prompt = render_compact_prompt_pack(case_id=case.case_id, model_name=model_name)
    if write:
        run_dir = work_dir / _safe_name(clean_run_id, fallback="local-run")
        if run_dir.exists() and not overwrite:
            raise LocalModelCandidateRunnerError(
                f"run directory already exists: {run_dir}; rerun with --overwrite after reviewing it"
            )
        if overwrite and run_dir.exists():
            shutil.rmtree(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
    else:
        temp_dir = tempfile.TemporaryDirectory(prefix="chili_local_model_candidate_run_")
        run_dir = Path(temp_dir.name)
    try:
        prompt_path = run_dir / "compact_prompt_pack.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        if response_file is not None:
            if not response_file.is_file():
                raise LocalModelCandidateRunnerError(f"response file does not exist: {response_file}")
            response_text = response_file.read_text(encoding="utf-8", errors="replace")
            duration_seconds = 0.0
            command = source_command or f"response imported from {response_file}"
        else:
            response_text, duration_seconds, command = _run_ollama(
                model_name=model_name,
                prompt=prompt,
                timeout_seconds=timeout_seconds,
            )
            if source_command:
                command = source_command
        response_path = run_dir / "model_response.txt"
        response_path.write_text(response_text, encoding="utf-8")
        payload = parse_model_response(response_text)
        raw_dir = run_dir / "raw"
        drop_path, patch_path = _write_candidate_drop(
            raw_dir=raw_dir,
            payload=payload,
            case_id=case.case_id,
            model_name=model_name,
            duration_seconds=duration_seconds,
        )
        recorder_summary = record_local_model_evidence(
            source_dir=source_dir,
            drop_dir=raw_dir,
            prompt_pack_path=prompt_path,
            response_path=response_path,
            model_name=model_name,
            run_id=clean_run_id,
            source_command=command,
            write=write,
            overwrite=overwrite,
        )
        summary = {
            "schema": LOCAL_MODEL_CANDIDATE_RUNNER_SCHEMA_VERSION,
            "generated_utc": _utc_now(),
            "status": "passed",
            "write": bool(write),
            "case_id": case.case_id,
            "model_name": model_name,
            "run_id": clean_run_id,
            "source_command": command,
            "run_dir": str(work_dir / _safe_name(clean_run_id, fallback="local-run")),
            "prompt_pack": str(source_dir / "prompt_pack.md"),
            "compact_prompt_pack": str(prompt_path if write else source_dir / "prompt_pack.md"),
            "response": str(response_path if write else source_dir / "model_response.txt"),
            "raw_drop": str(drop_path if write else source_dir / "raw" / drop_path.name),
            "raw_patch": str(patch_path if write else source_dir / "raw" / patch_path.name),
            "recorder": recorder_summary,
            "ready_source_count_delta": 1 if write else 0,
            "promotion_ready": False,
            "next_action": (
                "Run frontier intake with allow-partial for inspection, or collect matching "
                "Codex and Claude evidence before publishing promotion scorecards."
            ),
            "permission_boundary": (
                "local model candidate collection only; no source/test edits, git/PR action, "
                "runtime restart, deployment, database migration, broker call, or live trading"
            ),
        }
        return summary
    finally:
        if not write:
            temp_dir.cleanup()


def run_local_model_candidate_suite(
    *,
    source_dir: Path = DEFAULT_SOURCE_DIR,
    work_dir: Path = DEFAULT_WORK_DIR,
    model_name: str = DEFAULT_MODEL_NAME,
    response_file: Path | None = None,
    run_id: str | None = None,
    source_command: str | None = None,
    write: bool = True,
    overwrite: bool = False,
    timeout_seconds: int = 600,
) -> dict[str, object]:
    cases = default_cases()
    case_ids = [case.case_id for case in cases]
    clean_run_id = run_id or f"local-suite-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    prompt = render_compact_suite_prompt_pack(model_name=model_name)
    if write:
        run_dir = work_dir / _safe_name(clean_run_id, fallback="local-suite-run")
        if run_dir.exists() and not overwrite:
            raise LocalModelCandidateRunnerError(
                f"run directory already exists: {run_dir}; rerun with --overwrite after reviewing it"
            )
        if overwrite and run_dir.exists():
            shutil.rmtree(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
    else:
        temp_dir = tempfile.TemporaryDirectory(prefix="chili_local_model_candidate_suite_")
        run_dir = Path(temp_dir.name)
    try:
        prompt_path = run_dir / "full_prompt_pack.md"
        response_path = run_dir / "model_response.txt"
        prompt_chunks: list[str] = []
        response_chunks: list[str] = []
        case_results: list[dict[str, object]] = [
            {"case_id": case_id, "status": "pending"}
            for case_id in case_ids
        ]
        if response_file is not None:
            prompt_path.write_text(prompt, encoding="utf-8")
            if not response_file.is_file():
                raise LocalModelCandidateRunnerError(f"response file does not exist: {response_file}")
            response_text = response_file.read_text(encoding="utf-8", errors="replace")
            duration_seconds = 0.0
            command = source_command or f"response imported from {response_file}"
            response_path.write_text(response_text, encoding="utf-8")
            try:
                payloads = parse_model_response_suite(response_text, case_ids=case_ids)
            except LocalModelCandidateRunnerError as exc:
                for result in case_results:
                    result["status"] = "parse_failed"
                    result["error"] = str(exc)
                return _suite_failure_summary(
                    run_dir=run_dir,
                    work_dir=work_dir,
                    source_dir=source_dir,
                    clean_run_id=clean_run_id,
                    model_name=model_name,
                    command=command,
                    case_ids=case_ids,
                    case_results=case_results,
                    failure_stage="parse",
                    failure_reason=str(exc),
                    duration_seconds=duration_seconds,
                    write=write,
                    timeout_seconds=timeout_seconds,
                    prompt_path=prompt_path,
                    response_path=response_path,
                )
        else:
            payloads = []
            total_duration = 0.0
            case_run_dir = run_dir / "cases"
            case_run_dir.mkdir(parents=True, exist_ok=True)
            command = source_command or (
                f"ollama run {model_name} < compact_prompt_pack.md "
                f"(sequential all-cases: {', '.join(case_ids)})"
            )
            timeout_salvaged_cases: list[str] = []
            for index, case in enumerate(cases):
                case_prompt = render_compact_prompt_pack(
                    case_id=case.case_id,
                    model_name=model_name,
                )
                safe_case = _safe_name(case.case_id, fallback="case")
                case_prompt_path = case_run_dir / f"{safe_case}.prompt.md"
                case_response_path = case_run_dir / f"{safe_case}.response.txt"
                case_prompt_path.write_text(case_prompt, encoding="utf-8")
                prompt_chunks.extend(
                    [
                        f"<!-- sequential-case: {case.case_id} -->",
                        case_prompt,
                    ]
                )
                prompt_path.write_text("\n\n".join(prompt_chunks), encoding="utf-8")
                case_results[index].update(
                    {
                        "status": "prompt_written",
                        "prompt": str(case_prompt_path),
                    }
                )
                case_started = time.monotonic()
                try:
                    response_text, case_duration, _case_command = _run_ollama(
                        model_name=model_name,
                        prompt=case_prompt,
                        timeout_seconds=timeout_seconds,
                    )
                except LocalModelCandidateRunnerError as exc:
                    elapsed = time.monotonic() - case_started
                    total_duration += elapsed
                    case_results[index].update(
                        {
                            "status": "model_failed",
                            "duration_seconds": float(elapsed),
                            "error": str(exc),
                        }
                    )
                    return _suite_failure_summary(
                        run_dir=run_dir,
                        work_dir=work_dir,
                        source_dir=source_dir,
                        clean_run_id=clean_run_id,
                        model_name=model_name,
                        command=command,
                        case_ids=case_ids,
                        case_results=case_results,
                        failure_stage="model",
                        failure_reason=f"{case.case_id}: {exc}",
                        duration_seconds=total_duration,
                        write=write,
                        timeout_seconds=timeout_seconds,
                        prompt_path=prompt_path,
                        response_path=response_path,
                    )
                total_duration += case_duration
                case_response_path.write_text(response_text, encoding="utf-8")
                timeout_salvaged = "partial response captured" in _case_command
                if timeout_salvaged:
                    timeout_salvaged_cases.append(case.case_id)
                case_results[index].update(
                    {
                        "status": "response_recorded",
                        "response": str(case_response_path),
                        "duration_seconds": float(case_duration),
                        "source_command": _case_command,
                        "timeout_salvaged": timeout_salvaged,
                    }
                )
                response_chunks.extend(
                    [
                        f"===== case: {case.case_id} =====",
                        response_text,
                    ]
                )
                response_path.write_text("\n\n".join(response_chunks), encoding="utf-8")
                try:
                    payload = parse_model_response(response_text)
                except LocalModelCandidateRunnerError as exc:
                    case_results[index].update(
                        {
                            "status": "parse_failed",
                            "error": str(exc),
                        }
                    )
                    return _suite_failure_summary(
                        run_dir=run_dir,
                        work_dir=work_dir,
                        source_dir=source_dir,
                        clean_run_id=clean_run_id,
                        model_name=model_name,
                        command=command,
                        case_ids=case_ids,
                        case_results=case_results,
                        failure_stage="parse",
                        failure_reason=f"{case.case_id}: {exc}",
                        duration_seconds=total_duration,
                        write=write,
                        timeout_seconds=timeout_seconds,
                        prompt_path=prompt_path,
                        response_path=response_path,
                    )
                case_results[index]["status"] = "parsed"
                payloads.append(payload)
            if timeout_salvaged_cases:
                command += "; partial-timeout salvage: " + ", ".join(timeout_salvaged_cases)
            prompt = "\n\n".join(prompt_chunks)
            prompt_path.write_text(prompt, encoding="utf-8")
            response_path.write_text("\n\n".join(response_chunks), encoding="utf-8")
            duration_seconds = total_duration
        raw_dir = run_dir / "raw"
        raw_paths: list[tuple[Path, Path]] = []
        per_case_duration = duration_seconds / len(payloads) if payloads else duration_seconds
        try:
            for index, (case, payload) in enumerate(zip(cases, payloads, strict=True)):
                raw_paths.append(
                    _write_candidate_drop(
                        raw_dir=raw_dir,
                        payload=payload,
                        case_id=case.case_id,
                        model_name=model_name,
                        duration_seconds=per_case_duration,
                    )
                )
                if index < len(case_results):
                    case_results[index]["status"] = "recorded"
            recorder_summary = record_local_model_evidence(
                source_dir=source_dir,
                drop_dir=raw_dir,
                prompt_pack_path=prompt_path,
                response_path=response_path,
                model_name=model_name,
                run_id=clean_run_id,
                source_command=command,
                write=write,
                overwrite=overwrite,
            )
        except (LocalModelCandidateRunnerError, LocalModelEvidenceRecorderError) as exc:
            return _suite_failure_summary(
                run_dir=run_dir,
                work_dir=work_dir,
                source_dir=source_dir,
                clean_run_id=clean_run_id,
                model_name=model_name,
                command=command,
                case_ids=case_ids,
                case_results=case_results,
                failure_stage="record",
                failure_reason=str(exc),
                duration_seconds=duration_seconds,
                write=write,
                timeout_seconds=timeout_seconds,
                prompt_path=prompt_path,
                response_path=response_path,
            )
        raw_files = [
            str(path if write else source_dir / "raw" / path.name)
            for pair in raw_paths
            for path in pair
        ]
        return {
            "schema": LOCAL_MODEL_CANDIDATE_RUNNER_SCHEMA_VERSION,
            "generated_utc": _utc_now(),
            "status": "passed",
            "write": bool(write),
            "case_id": "all",
            "case_ids": case_ids,
            "cases": len(case_ids),
            "model_name": model_name,
            "run_id": clean_run_id,
            "source_command": command,
            "run_dir": str(work_dir / _safe_name(clean_run_id, fallback="local-suite-run")),
            "prompt_pack": str(source_dir / "prompt_pack.md"),
            "compact_prompt_pack": "",
            "full_prompt_pack": str(prompt_path if write else source_dir / "prompt_pack.md"),
            "response": str(response_path if write else source_dir / "model_response.txt"),
            "raw_files": raw_files,
            "case_results": [dict(item) for item in case_results],
            "timeout_salvaged_cases": timeout_salvaged_cases if response_file is None else [],
            "recorder": recorder_summary,
            "ready_source_count_delta": 1 if write else 0,
            "promotion_ready": False,
            "next_action": (
                "Run frontier intake with allow-partial for inspection, or collect matching "
                "Codex and Claude evidence before publishing promotion scorecards."
            ),
            "permission_boundary": (
                "local model suite candidate collection only; no source/test edits, git/PR action, "
                "runtime restart, deployment, database migration, broker call, or live trading"
            ),
        }
    finally:
        if not write:
            temp_dir.cleanup()


def render_run_summary(summary: Mapping[str, object]) -> str:
    lines = [
        "# CHILI Local Model Candidate Run",
        "",
        f"- Schema: {summary.get('schema')}",
        f"- Generated UTC: {summary.get('generated_utc')}",
        f"- Status: {summary.get('status')}",
        f"- Write mode: {summary.get('write')}",
        f"- Case: {summary.get('case_id')}",
        f"- Cases: {summary.get('cases') or 1}",
        f"- Model: {summary.get('model_name')}",
        f"- Run id: {summary.get('run_id')}",
        f"- Promotion ready: {summary.get('promotion_ready')}",
        f"- Next action: {summary.get('next_action')}",
        f"- Permission boundary: {summary.get('permission_boundary')}",
    ]
    if summary.get("failure_reason"):
        lines.extend(
            [
                f"- Failure stage: {summary.get('failure_stage')}",
                f"- Failure reason: {summary.get('failure_reason')}",
                f"- Attempted cases: {summary.get('attempted_cases')}",
                f"- Parsed cases: {summary.get('parsed_cases')}",
                f"- Failed case: {summary.get('failed_case_id') or 'unknown'}",
            ]
        )
    lines.extend(
        [
            "",
            "| Artifact | Path |",
            "| --- | --- |",
        ]
    )
    for label, key in (
        ("prompt_pack", "prompt_pack"),
        ("compact_prompt_pack", "compact_prompt_pack"),
        ("full_prompt_pack", "full_prompt_pack"),
        ("response", "response"),
        ("raw_drop", "raw_drop"),
        ("raw_patch", "raw_patch"),
        ("diagnostics", "diagnostics"),
        ("retry_from_diagnostics", "retry_from_diagnostics"),
    ):
        value = summary.get(key)
        if value:
            lines.append(f"| {_escape_cell(label)} | {_escape_cell(str(value))} |")
    raw_files = summary.get("raw_files")
    if isinstance(raw_files, list):
        lines.append(f"| raw_files | {_escape_cell(str(len(raw_files)))} |")
    recovery_routes = [
        route
        for route in (summary.get("recovery_routes") or [])
        if isinstance(route, Mapping)
    ]
    if recovery_routes:
        lines.extend(
            [
                "",
                "## Recovery Routes",
                "",
                "| Action | Case | Retry command | Import response command | Boundary |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for route in recovery_routes:
            lines.append(
                "| "
                + " | ".join(
                    _escape_cell(
                        str(
                            route.get(key)
                            or ""
                        )
                    )
                    for key in (
                        "action_label",
                        "case_id",
                        "retry_command",
                        "import_response_command",
                        "permission_boundary",
                    )
                )
                + " |"
            )
    timeout_salvaged_cases = [
        str(case_id).strip()
        for case_id in (summary.get("timeout_salvaged_cases") or [])
        if str(case_id).strip()
    ]
    if timeout_salvaged_cases:
        lines.extend(
            [
                "",
                "## Timeout Salvage",
                "",
                f"- Timeout salvaged count: {len(timeout_salvaged_cases)}",
                f"- Timeout salvaged cases: {', '.join(timeout_salvaged_cases)}",
                "- Meaning: the local model process timed out, but CHILI captured a complete candidate from partial output and recorded it with provenance.",
            ]
        )
    lines.append("")
    return "\n".join(lines)


def write_summary(markdown: str, output_path: Path = DEFAULT_OUTPUT) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    return output_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run or import compact local-model candidate evidence and record provenance."
    )
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR)
    parser.add_argument("--case-id", default=DEFAULT_CASE_ID)
    parser.add_argument("--all-cases", action="store_true")
    parser.add_argument("--model-name", default=DEFAULT_MODEL_NAME)
    parser.add_argument("--response-file", type=Path)
    parser.add_argument(
        "--retry-from-diagnostics",
        type=Path,
        help="Retry or import the failed case named by a suite_diagnostics.json file.",
    )
    parser.add_argument("--run-id")
    parser.add_argument("--source-command")
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        retry_context: dict[str, str] = {}
        if args.retry_from_diagnostics is not None:
            if args.all_cases:
                raise LocalModelCandidateRunnerError(
                    "use --retry-from-diagnostics for the failed case only; rerun --all-cases after it parses"
                )
            retry_context = _suite_retry_context_from_diagnostics(
                args.retry_from_diagnostics
            )
        if args.all_cases:
            summary = run_local_model_candidate_suite(
                source_dir=args.source_dir,
                work_dir=args.work_dir,
                model_name=args.model_name,
                response_file=args.response_file,
                run_id=args.run_id,
                source_command=args.source_command,
                write=not args.no_write,
                overwrite=args.overwrite,
                timeout_seconds=args.timeout_seconds,
            )
        else:
            case_id = (
                args.case_id
                if args.case_id != DEFAULT_CASE_ID or not retry_context
                else retry_context["case_id"]
            )
            model_name = (
                args.model_name
                if args.model_name != DEFAULT_MODEL_NAME or not retry_context
                else retry_context["model_name"]
            )
            run_id = args.run_id
            if retry_context and not run_id:
                run_id = (
                    f"local-retry-{_safe_name(case_id, fallback='case')}-"
                    f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
                )
            source_command = args.source_command
            if retry_context and not source_command:
                if args.response_file is not None:
                    source_command = (
                        f"response imported from {args.response_file} "
                        f"for failed case in {retry_context['diagnostics']}"
                    )
                else:
                    source_command = (
                        f"ollama run {model_name} < compact_prompt_pack.md "
                        f"(retry from {retry_context['diagnostics']})"
                    )
            summary = run_local_model_candidate_case(
                source_dir=args.source_dir,
                work_dir=args.work_dir,
                case_id=case_id,
                model_name=model_name,
                response_file=args.response_file,
                run_id=run_id,
                source_command=source_command,
                write=not args.no_write,
                overwrite=args.overwrite,
                timeout_seconds=args.timeout_seconds,
            )
            if retry_context:
                summary = dict(summary)
                summary["retry_from_diagnostics"] = retry_context["diagnostics"]
                summary["retry_source_run_id"] = retry_context["run_id"]
    except (LocalModelCandidateRunnerError, LocalModelEvidenceRecorderError) as exc:
        print(f"local model candidate runner error: {exc}", file=sys.stderr)
        return 2

    markdown = render_run_summary(summary)
    if not args.no_write:
        write_summary(markdown, args.output)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(markdown)
        if not args.no_write:
            print(f"Wrote {args.output}")
    return 0 if summary.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
