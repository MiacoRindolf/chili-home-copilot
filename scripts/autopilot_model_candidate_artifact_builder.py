from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.autopilot_model_candidate_artifact_bakeoff import (  # noqa: E402
    ALLOWED_SOURCE_KINDS,
    EVALUATION_MODE_ACTUAL,
    MODEL_CANDIDATE_ARTIFACT_SCHEMA_VERSION,
    _candidate_to_artifact,
)
from scripts.autopilot_real_chili_candidate_bakeoff import (  # noqa: E402
    REAL_CHILI_CANDIDATE_BAKEOFF_SCHEMA_VERSION,
    REQUIRED_COMPARISON_CLASSES,
    default_cases as real_chili_default_cases,
    missing_comparison_classes,
)


DEFAULT_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "MODEL_CANDIDATE_ARTIFACTS.json"
DEFAULT_PROMPT_PACK_OUTPUT = (
    REPO_ROOT / "project_ws" / "AgentOps" / "MODEL_CANDIDATE_DROP_PROMPT_PACK.md"
)
MODEL_CANDIDATE_DROP_SCHEMA_VERSION = "chili.model-candidate-drop.v1"
MODEL_CANDIDATE_ARTIFACT_BUILDER_SCHEMA_VERSION = "chili.model-candidate-artifact-builder.v1"
MODEL_CANDIDATE_DROP_PROMPT_PACK_SCHEMA_VERSION = "chili.model-candidate-drop-prompt-pack.v1"
MODEL_CANDIDATE_DROP_PROVENANCE_SCHEMA_VERSION = "chili.model-candidate-drop-provenance.v1"
TRANSCRIPT_MIN_EVENTS = 3
FRONTIER_IDENTITY_SOURCE_KINDS = frozenset({"codex", "claude"})
PROVIDER_IDENTITY_SOURCES = frozenset({"provider_export", "provider_native", "provider_ui_capture"})
SOURCE_SPECIFIC_PROMPT_CONTRACTS = {
    "codex": (
        "Source contract: hosted-codex-frontier-candidate",
        "Collector source: codex",
        "Use the hosted Codex session as transcript evidence, but do not treat Codex PR state or ready claims as proof without current-head receipts.",
        "Return one minimal unified-diff patch per case and keep external reasoning outside the patch/drop files.",
    ),
    "claude": (
        "Source contract: hosted-claude-frontier-candidate",
        "Collector source: claude",
        "Use the hosted Claude session as transcript evidence, but keep assertions tied to the fixture files and required behavior command.",
        "Promotion requires the original response to match the provider-native or response-hash-bound provider-UI Fable 5 event; a recorder-declared model label or unrelated Fable event is not identity evidence.",
        "Return one minimal unified-diff patch per case and avoid broad rewrites when the fixture has a smaller repair path.",
    ),
    "local_model": (
        "Source contract: local-model-frontier-candidate",
        "Collector source: local_model",
        "Keep context compact: rely only on the active case, fixture files, drop template, and required behavior command.",
        "Return one minimal unified-diff patch per case; if uncertain, emit a rejected/incomplete drop rather than inventing hidden context.",
    ),
    "other": (
        "Source contract: external-frontier-candidate",
        "Collector source: other",
        "Use the external model session as transcript evidence and keep every claim tied to the fixture files and required behavior command.",
        "Return one minimal unified-diff patch per case with a complete provenance block.",
    ),
}


class ArtifactBuildError(ValueError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ArtifactBuildError(f"{label} must be an object")
    return value


def _required_text(payload: Mapping[str, object], key: str, *, label: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ArtifactBuildError(f"{label}.{key} is required")
    return value


def _number(payload: Mapping[str, object], key: str, *, label: str, default: float) -> float:
    value = payload.get(key, default)
    if not isinstance(value, (int, float)):
        raise ArtifactBuildError(f"{label}.{key} must be a number")
    return float(value)


def _text_list(
    payload: Mapping[str, object],
    key: str,
    *,
    label: str,
    default: Sequence[str] = (),
) -> list[str]:
    value = payload.get(key, list(default))
    if not isinstance(value, list):
        raise ArtifactBuildError(f"{label}.{key} must be a list")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ArtifactBuildError(f"{label}.{key} contains a blank value")
        out.append(item.strip())
    return out


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _transcript_event_count(path: Path, *, label: str, required: bool) -> int:
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if required and len(lines) < TRANSCRIPT_MIN_EVENTS:
        raise ArtifactBuildError(
            f"{label}.provenance.transcript_file must contain at least {TRANSCRIPT_MIN_EVENTS} non-empty events"
        )
    if required:
        for index, line in enumerate(lines, start=1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ArtifactBuildError(
                    f"{label}.provenance.transcript_file event {index} must be valid JSON"
                ) from exc
            if not isinstance(event, Mapping):
                raise ArtifactBuildError(
                    f"{label}.provenance.transcript_file event {index} must be an object"
                )
    return len(lines)


def _normalized_model_name(value: object) -> str:
    return "-".join(str(value or "").strip().lower().replace("_", "-").split())


def _model_identity_matches(source_kind: str, expected: str, observed: str) -> bool:
    expected_name = _normalized_model_name(expected)
    observed_name = _normalized_model_name(observed)
    if not expected_name or not observed_name:
        return False
    if source_kind == "claude" and expected_name == "claude-fable-5":
        return observed_name == expected_name or observed_name.startswith(expected_name + "-")
    if source_kind == "codex" and expected_name == "gpt-5.5":
        return observed_name == expected_name or observed_name.startswith(expected_name + "-")
    return expected_name == observed_name


def _normalized_response_text(value: object) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _assistant_event_text(event: Mapping[str, object]) -> str:
    message = event.get("message")
    if isinstance(message, Mapping):
        role = str(message.get("role") or "").strip().lower()
        if role != "assistant":
            return ""
        content = message.get("content")
    else:
        role = str(event.get("role") or "").strip().lower()
        if role != "assistant":
            return ""
        content = event.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, Mapping)
            and str(block.get("type") or "").strip().lower() == "text"
            and str(block.get("text") or "").strip()
        )
    return ""


def _transcript_model_identity(
    path: Path,
    *,
    source_kind: str,
    expected_model_name: str,
    expected_response_text: str | None = None,
    expected_response_sha256: str = "",
    require_response_binding: bool = False,
) -> dict[str, object]:
    observed: list[str] = []
    identity_sources: list[str] = []
    response_models: list[str] = []
    expected_text = (
        _normalized_response_text(expected_response_text)
        if expected_response_text is not None
        else None
    )
    expected_sha256 = str(expected_response_sha256 or "").strip().lower()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, Mapping):
            continue
        message = event.get("message")
        if isinstance(message, Mapping):
            model = message.get("model")
            if isinstance(model, str) and model.strip() and not model.strip().startswith("<"):
                clean_model = model.strip()
                observed.append(clean_model)
                identity_sources.append("provider_native_message")
                assistant_text = _normalized_response_text(_assistant_event_text(event))
                if expected_text is not None and assistant_text == expected_text:
                    response_models.append(clean_model)
        identity_source = str(event.get("identity_source") or "").strip().lower()
        provider_model = event.get("provider_model_name")
        if (
            identity_source in PROVIDER_IDENTITY_SOURCES
            and isinstance(provider_model, str)
            and provider_model.strip()
        ):
            clean_model = provider_model.strip()
            observed.append(clean_model)
            identity_sources.append(identity_source)
            response_sha256 = str(event.get("response_sha256") or "").strip().lower()
            if expected_sha256 and response_sha256 == expected_sha256:
                response_models.append(clean_model)
    unique_models = list(dict.fromkeys(observed))
    unique_response_models = list(dict.fromkeys(response_models))
    session_verified = source_kind not in FRONTIER_IDENTITY_SOURCE_KINDS or any(
        _model_identity_matches(source_kind, expected_model_name, model)
        for model in unique_models
    )
    response_bound = bool(unique_response_models)
    response_verified = response_bound and all(
        _model_identity_matches(source_kind, expected_model_name, model)
        for model in unique_response_models
    )
    verified = response_verified if require_response_binding else session_verified
    return {
        "model_identity_expected": expected_model_name,
        "model_identity_models": unique_models,
        "model_identity_sources": list(dict.fromkeys(identity_sources)),
        "model_identity_session_verified": session_verified,
        "model_identity_response_bound": response_bound,
        "model_identity_response_models": unique_response_models,
        "model_identity_scope": (
            "candidate_response"
            if response_bound
            else "unbound"
            if require_response_binding
            else "transcript_session"
        ),
        "model_identity_verified": verified,
    }


def _required_sha256(payload: Mapping[str, object], key: str, *, label: str) -> str:
    value = _required_text(payload, key, label=label).lower()
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ArtifactBuildError(f"{label}.{key} must be a SHA-256 hex digest")
    return value


def _safe_relative_file(base_dir: Path, raw_path: str, *, field_name: str = "patch_file") -> Path:
    candidate = Path(raw_path)
    if candidate.is_absolute():
        raise ArtifactBuildError(f"{field_name} must be relative to its drop JSON")
    resolved_base = base_dir.resolve()
    resolved = (resolved_base / candidate).resolve()
    if resolved_base not in resolved.parents and resolved != resolved_base:
        raise ArtifactBuildError(f"{field_name} escapes its drop directory")
    if not resolved.is_file():
        raise ArtifactBuildError(f"{field_name} does not exist: {raw_path}")
    return resolved


def _patch_text(payload: Mapping[str, object], *, drop_dir: Path, label: str) -> str:
    inline = payload.get("patch")
    patch_file = payload.get("patch_file")
    if isinstance(inline, str) and inline.strip():
        return inline
    if isinstance(patch_file, str) and patch_file.strip():
        return _safe_relative_file(drop_dir, patch_file).read_text(encoding="utf-8")
    raise ArtifactBuildError(f"{label}.patch or {label}.patch_file is required")


def validate_drop_provenance(
    drop: Mapping[str, object],
    *,
    drop_dir: Path,
    label: str,
    required: bool = False,
    prompt_pack_path: Path | None = None,
) -> dict[str, object] | None:
    raw = drop.get("provenance")
    if raw is None:
        if required:
            raise ArtifactBuildError(f"{label}.provenance is required")
        return None
    provenance = _as_mapping(raw, label=f"{label}.provenance")
    schema = provenance.get("schema")
    if schema != MODEL_CANDIDATE_DROP_PROVENANCE_SCHEMA_VERSION:
        raise ArtifactBuildError(
            f"{label}.provenance.schema is {schema or 'missing'} instead of "
            f"{MODEL_CANDIDATE_DROP_PROVENANCE_SCHEMA_VERSION}"
        )
    prompt_pack_schema = _required_text(provenance, "prompt_pack_schema", label=f"{label}.provenance")
    if prompt_pack_schema != MODEL_CANDIDATE_DROP_PROMPT_PACK_SCHEMA_VERSION:
        raise ArtifactBuildError(
            f"{label}.provenance.prompt_pack_schema is {prompt_pack_schema} instead of "
            f"{MODEL_CANDIDATE_DROP_PROMPT_PACK_SCHEMA_VERSION}"
        )

    source_kind = _required_text(drop, "source_kind", label=label)
    model_name = _required_text(drop, "model_name", label=label)
    run_id = _required_text(provenance, "run_id", label=f"{label}.provenance")
    collector = _required_text(provenance, "collector", label=f"{label}.provenance")
    source_command = _required_text(provenance, "source_command", label=f"{label}.provenance")
    prompt_pack_sha256 = _required_sha256(
        provenance,
        "prompt_pack_sha256",
        label=f"{label}.provenance",
    )
    prompt_pack_file = provenance.get("prompt_pack_file")
    transcript_file = _required_text(provenance, "transcript_file", label=f"{label}.provenance")
    transcript_sha256 = _required_sha256(
        provenance,
        "transcript_sha256",
        label=f"{label}.provenance",
    )

    transcript_path = _safe_relative_file(
        drop_dir,
        transcript_file,
        field_name="provenance.transcript_file",
    )
    actual_transcript_sha256 = sha256_file(transcript_path)
    if actual_transcript_sha256 != transcript_sha256:
        raise ArtifactBuildError(
            f"{label}.provenance.transcript_sha256 mismatch: "
            f"expected {transcript_sha256}, got {actual_transcript_sha256}"
        )
    transcript_events = _transcript_event_count(
        transcript_path,
        label=label,
        required=required,
    )
    provider_response_file = provenance.get("provider_response_file")
    provider_response_sha256 = provenance.get("provider_response_sha256")
    provider_response_text: str | None = None
    provider_response_verified = False
    verified_provider_response_file: str | None = None
    if provider_response_file is not None or provider_response_sha256 is not None:
        if not isinstance(provider_response_file, str) or not provider_response_file.strip():
            raise ArtifactBuildError(
                f"{label}.provenance.provider_response_file is required"
            )
        response_sha256 = _required_sha256(
            provenance,
            "provider_response_sha256",
            label=f"{label}.provenance",
        )
        response_path = _safe_relative_file(
            drop_dir,
            provider_response_file,
            field_name="provenance.provider_response_file",
        )
        actual_response_sha256 = sha256_file(response_path)
        if actual_response_sha256 != response_sha256:
            raise ArtifactBuildError(
                f"{label}.provenance.provider_response_sha256 mismatch: "
                f"expected {response_sha256}, got {actual_response_sha256}"
            )
        provider_response_text = response_path.read_text(
            encoding="utf-8",
            errors="replace",
        )
        provider_response_sha256 = response_sha256
        provider_response_verified = True
        verified_provider_response_file = provider_response_file.strip()
    model_identity = _transcript_model_identity(
        transcript_path,
        source_kind=source_kind,
        expected_model_name=model_name,
        expected_response_text=provider_response_text,
        expected_response_sha256=str(provider_response_sha256 or ""),
        require_response_binding=source_kind in FRONTIER_IDENTITY_SOURCE_KINDS,
    )

    prompt_pack_verified = False
    verified_prompt_pack_file: str | None = None
    if prompt_pack_path is None and isinstance(prompt_pack_file, str) and prompt_pack_file.strip():
        prompt_pack_path = _safe_relative_file(
            drop_dir,
            prompt_pack_file,
            field_name="provenance.prompt_pack_file",
        )
        verified_prompt_pack_file = prompt_pack_file.strip()
    if prompt_pack_path is None:
        if required:
            raise ArtifactBuildError(
                f"{label}.provenance prompt_pack_path or prompt_pack_file is required when provenance is required"
            )
    else:
        if not prompt_pack_path.is_file():
            raise ArtifactBuildError(f"prompt_pack does not exist: {prompt_pack_path}")
        actual_prompt_pack_sha256 = sha256_file(prompt_pack_path)
        if actual_prompt_pack_sha256 != prompt_pack_sha256:
            raise ArtifactBuildError(
                f"{label}.provenance.prompt_pack_sha256 mismatch: "
                f"expected {prompt_pack_sha256}, got {actual_prompt_pack_sha256}"
            )
        validate_prompt_pack_markdown(
            prompt_pack_path.read_text(encoding="utf-8", errors="replace"),
            source_kind=source_kind,
            model_name=model_name,
            label=f"{label}.provenance.prompt_pack_file",
        )
        prompt_pack_verified = True

    return {
        "schema": MODEL_CANDIDATE_DROP_PROVENANCE_SCHEMA_VERSION,
        "prompt_pack_schema": prompt_pack_schema,
        "prompt_pack_sha256": prompt_pack_sha256,
        "prompt_pack_file": verified_prompt_pack_file,
        "prompt_pack_verified": prompt_pack_verified,
        "run_id": run_id,
        "collector": collector,
        "source_command": source_command,
        "transcript_file": transcript_file,
        "transcript_sha256": transcript_sha256,
        "transcript_verified": True,
        "transcript_events": transcript_events,
        "transcript_size_bytes": transcript_path.stat().st_size,
        "provider_response_file": verified_provider_response_file,
        "provider_response_sha256": (
            str(provider_response_sha256) if provider_response_verified else None
        ),
        "provider_response_verified": provider_response_verified,
        **model_identity,
    }


def load_drop(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ArtifactBuildError(f"{path}: invalid JSON: {exc}") from exc
    payload = dict(_as_mapping(raw, label=str(path)))
    payload["_drop_dir"] = str(path.parent)
    payload["_drop_path"] = str(path)
    return payload


def load_drops(drop_dir: Path) -> list[dict[str, object]]:
    if not drop_dir.is_dir():
        raise ArtifactBuildError(f"drop directory does not exist: {drop_dir}")
    paths = sorted(path for path in drop_dir.rglob("*.json") if path.is_file())
    if not paths:
        raise ArtifactBuildError(f"drop directory has no JSON drops: {drop_dir}")
    return [load_drop(path) for path in paths]


def _synthetic_drop_from_case(case_id: str, patch: str, planned_file: str, command: str) -> dict[str, object]:
    return {
        "schema": MODEL_CANDIDATE_DROP_SCHEMA_VERSION,
        "case_id": case_id,
        "candidate_id": f"codex-collected-{case_id}",
        "model_name": "codex-collected-candidate",
        "source_kind": "codex",
        "collected_at": "2026-06-02T00:00:00Z",
        "patch": patch,
        "planned_file": planned_file,
        "expected_changed_files": [planned_file],
        "declared_commands": [command],
        "duration_seconds": 10.0,
        "cost_units": 10.0,
        "_drop_dir": str(REPO_ROOT),
        "_drop_path": "<self-test>",
    }


def synthetic_drops() -> list[dict[str, object]]:
    drops: list[dict[str, object]] = []
    for case in real_chili_default_cases():
        drops.append(
            _synthetic_drop_from_case(
                case.case_id,
                case.incumbent.patch,
                case.incumbent.planned_file,
                case.incumbent.declared_commands[0],
            )
        )
    return drops


def _candidate_from_drop(
    drop: Mapping[str, object],
    *,
    base_expected_files: Sequence[str],
    allow_fixture: bool,
    index: int,
    require_provenance: bool = False,
    prompt_pack_path: Path | None = None,
) -> dict[str, object]:
    label = f"drop[{index}]"
    schema = drop.get("schema")
    if schema != MODEL_CANDIDATE_DROP_SCHEMA_VERSION:
        raise ArtifactBuildError(
            f"{label}.schema is {schema or 'missing'} instead of {MODEL_CANDIDATE_DROP_SCHEMA_VERSION}"
        )
    source_kind = _required_text(drop, "source_kind", label=label)
    if source_kind not in ALLOWED_SOURCE_KINDS:
        raise ArtifactBuildError(
            f"{label}.source_kind must be one of {', '.join(ALLOWED_SOURCE_KINDS)}"
        )
    if source_kind == "fixture" and not allow_fixture:
        raise ArtifactBuildError(f"{label}.source_kind fixture is not allowed for collected drops")
    planned_file = _required_text(drop, "planned_file", label=label)
    drop_dir = Path(str(drop.get("_drop_dir") or "."))
    provenance = validate_drop_provenance(
        drop,
        drop_dir=drop_dir,
        label=label,
        required=require_provenance,
        prompt_pack_path=prompt_pack_path,
    )
    candidate = {
        "candidate_id": _required_text(drop, "candidate_id", label=label),
        "model_name": _required_text(drop, "model_name", label=label),
        "source_kind": source_kind,
        "collected_at": str(drop.get("collected_at") or _utc_now()),
        "patch": _patch_text(drop, drop_dir=drop_dir, label=label),
        "planned_file": planned_file,
        "expected_changed_files": _text_list(
            drop,
            "expected_changed_files",
            label=label,
            default=base_expected_files or (planned_file,),
        ),
        "declared_commands": _text_list(drop, "declared_commands", label=label),
        "duration_seconds": _number(drop, "duration_seconds", label=label, default=0.0),
        "cost_units": _number(drop, "cost_units", label=label, default=0.0),
    }
    if provenance is not None:
        candidate["provenance"] = provenance
    return candidate


def build_artifact(
    drops: Sequence[Mapping[str, object]],
    *,
    allow_partial: bool = False,
    allow_fixture: bool = False,
    require_provenance: bool = False,
    prompt_pack_path: Path | None = None,
) -> dict[str, object]:
    base_cases = {case.case_id: case for case in real_chili_default_cases()}
    seen: set[str] = set()
    entries: list[dict[str, object]] = []
    for index, drop in enumerate(drops, start=1):
        case_id = _required_text(drop, "case_id", label=f"drop[{index}]")
        if case_id in seen:
            raise ArtifactBuildError(f"duplicate case_id: {case_id}")
        seen.add(case_id)
        base = base_cases.get(case_id)
        if base is None:
            raise ArtifactBuildError(f"unknown case_id: {case_id}")
        challenger = _candidate_from_drop(
            drop,
            base_expected_files=base.incumbent.expected_changed_files,
            allow_fixture=allow_fixture,
            index=index,
            require_provenance=require_provenance,
            prompt_pack_path=prompt_pack_path,
        )
        entries.append(
            {
                "case_id": base.case_id,
                "comparison_class": base.bakeoff_class,
                "incumbent": _candidate_to_artifact(
                    base.incumbent,
                    model_name="chili-incumbent-fixture",
                    collected_at=str(drop.get("collected_at") or _utc_now()),
                ),
                "challenger": challenger,
            }
        )

    probe_cases = [base_cases[str(entry["case_id"])] for entry in entries]
    missing = missing_comparison_classes(probe_cases)
    if missing and not allow_partial:
        raise ArtifactBuildError("missing comparison classes: " + ", ".join(missing))
    return {
        "schema": MODEL_CANDIDATE_ARTIFACT_SCHEMA_VERSION,
        "evaluation_mode": EVALUATION_MODE_ACTUAL,
        "generated_utc": _utc_now(),
        "source": "collected model candidate drops",
        "builder_schema": MODEL_CANDIDATE_ARTIFACT_BUILDER_SCHEMA_VERSION,
        "drop_schema": MODEL_CANDIDATE_DROP_SCHEMA_VERSION,
        "base_benchmark_schema": REAL_CHILI_CANDIDATE_BAKEOFF_SCHEMA_VERSION,
        "required_comparison_classes": list(REQUIRED_COMPARISON_CLASSES),
        "entries": entries,
    }


def write_artifact(artifact: Mapping[str, object], output_path: Path = DEFAULT_OUTPUT) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output_path


def _command_text(command: Sequence[str]) -> str:
    return " ".join(str(part) for part in command)


def _drop_template(
    case_id: str,
    planned_file: str,
    command: str,
    source_kind: str,
    model_name: str,
    *,
    response_only: bool = False,
) -> dict[str, object]:
    template: dict[str, object] = {
        "schema": MODEL_CANDIDATE_DROP_SCHEMA_VERSION,
        "case_id": case_id,
        "candidate_id": f"{source_kind}-{case_id}",
        "model_name": model_name,
        "source_kind": source_kind,
        "patch": "<unified diff for the planned file only>",
        "planned_file": planned_file,
        "expected_changed_files": [planned_file],
        "declared_commands": [command],
        "duration_seconds": 0.0,
        "cost_units": 0.0,
        "notes": "<short explanation>",
    }
    if response_only:
        return template
    template.pop("patch")
    template.pop("notes")
    template.update(
        {
            "collected_at": "<UTC ISO-8601 timestamp>",
            "patch_file": f"{case_id}.patch",
        }
    )
    template["provenance"] = {
        "schema": MODEL_CANDIDATE_DROP_PROVENANCE_SCHEMA_VERSION,
        "prompt_pack_schema": MODEL_CANDIDATE_DROP_PROMPT_PACK_SCHEMA_VERSION,
        "prompt_pack_file": f"{case_id}.prompt-pack.md",
        "prompt_pack_sha256": "<sha256 of saved prompt pack markdown>",
        "run_id": "<external model run id>",
        "collector": "<collector name or tool>",
        "source_command": "<model invocation or UI session label>",
        "transcript_file": f"{case_id}.transcript.jsonl",
        "transcript_sha256": "<sha256 of transcript file>",
    }
    return template


def source_specific_prompt_contract(
    source_kind: str,
    model_name: str,
    *,
    response_only: bool = False,
) -> tuple[str, ...]:
    if source_kind not in ALLOWED_SOURCE_KINDS or source_kind == "fixture":
        raise ArtifactBuildError(
            "prompt-pack source_kind must be one of codex, claude, local_model, other"
        )
    base = SOURCE_SPECIFIC_PROMPT_CONTRACTS.get(
        source_kind,
        SOURCE_SPECIFIC_PROMPT_CONTRACTS["other"],
    )
    return (
        *base,
        f"Model identity: {model_name}",
        (
            "Do not create files, run commands, compute hashes, or include provenance; "
            "return only the JSON objects and CHILI records provenance after parsing."
            if response_only
            else "Every transcript must include the prompt-pack SHA-256, source kind, model name, case id, and final patch/drop decision."
        ),
    )


def validate_prompt_pack_markdown(
    markdown: str,
    *,
    source_kind: str,
    model_name: str,
    label: str = "prompt_pack",
    response_only: bool = False,
) -> None:
    required_fragments = (
        f"- Schema: {MODEL_CANDIDATE_DROP_PROMPT_PACK_SCHEMA_VERSION}",
        f"- Source kind: {source_kind}",
        f"- Model name: {model_name}",
        "## Source-Specific Operating Contract",
        f'"source_kind": "{source_kind}"',
        f'"model_name": "{model_name}"',
        *(
            f"- {fragment}"
            for fragment in source_specific_prompt_contract(
                source_kind,
                model_name,
                response_only=response_only,
            )
        ),
    )
    if response_only:
        required_fragments = (
            *required_fragments,
            "- Do not emit `patch_file`, `collected_at`, or `provenance`; the local recorder writes files and provenance after dry-run validation.",
            '"patch": "<unified diff for the planned file only>"',
        )
    missing = [fragment for fragment in required_fragments if fragment not in markdown]
    if missing:
        raise ArtifactBuildError(
            f"{label} is missing required fragments: " + ", ".join(missing)
        )


def render_prompt_pack(
    *,
    source_kind: str = "codex",
    model_name: str = "candidate-model",
    generated_at: datetime | None = None,
    response_only: bool = False,
) -> str:
    if source_kind not in ALLOWED_SOURCE_KINDS or source_kind == "fixture":
        raise ArtifactBuildError(
            "prompt-pack source_kind must be one of codex, claude, local_model, other"
        )
    generated_at = generated_at or datetime.now(timezone.utc)
    lines = [
        "# CHILI Model Candidate Drop Prompt Pack",
        "",
        f"- Schema: {MODEL_CANDIDATE_DROP_PROMPT_PACK_SCHEMA_VERSION}",
        f"- Drop schema: {MODEL_CANDIDATE_DROP_SCHEMA_VERSION}",
        f"- Generated UTC: {generated_at.isoformat().replace('+00:00', 'Z')}",
        f"- Source kind: {source_kind}",
        f"- Model name: {model_name}",
        f"- Cases: {len(real_chili_default_cases())}",
        (
            "- Required behavior: return one valid JSON object per case, each with an inline unified-diff `patch` string."
            if response_only
            else "- Required behavior: produce one unified-diff patch file and one candidate-drop JSON file per case."
        ),
        "- Safety: work only from the temporary fixture text below; do not touch the real checkout, git state, runtime, database, broker, deployment, credentials, or live-trading controls.",
        "",
        "## Output Contract" if response_only else "## Collection Rules",
        "",
    ]
    if response_only:
        lines.extend(
            [
                "- Return only JSON objects, either newline-delimited JSON or objects inside one JSON array.",
                "- Return exactly one object for each case section below; no prose, Markdown fences, PR summaries, or readiness claims.",
                "- Keep each patch scoped to the listed planned file.",
                "- Include the listed behavior command exactly in `declared_commands`.",
                "- Use `source_kind` exactly as shown above; do not use `fixture` for real model output.",
                "- The `patch` value must start with `diff --git` and must be inline in the JSON object.",
                "- Do not emit `patch_file`, `collected_at`, or `provenance`; the local recorder writes files and provenance after dry-run validation.",
                "- Do not run commands, compute SHA-256 values, inspect the real checkout, or try to write artifacts.",
                "- If you are uncertain, still return your best scoped patch; CHILI will reject unsafe or failing candidates.",
            ]
        )
    else:
        lines.extend(
            [
                "- Keep each patch scoped to the listed planned file.",
                "- Include the listed behavior command exactly in `declared_commands`.",
                "- Use `source_kind` exactly as shown above; do not use `fixture` for real model output.",
                "- Put each JSON drop next to its patch file so `patch_file` stays repo-safe and relative.",
                f"- Put a transcript with at least {TRANSCRIPT_MIN_EVENTS} non-empty JSONL events next to each JSON drop and fill the `provenance` block with the prompt-pack SHA-256 and transcript SHA-256.",
                "- If a case is ambiguous or cannot be solved, still emit a JSON drop with an empty patch file path omitted and explain the blocker in external notes; CHILI will reject incomplete drops instead of guessing.",
            ]
        )
    lines.extend(
        [
            "",
            "## Source-Specific Operating Contract",
            "",
        ]
    )
    lines.extend(
        f"- {item}"
        for item in source_specific_prompt_contract(
            source_kind,
            model_name,
            response_only=response_only,
        )
    )
    lines.append("")
    for case in real_chili_default_cases():
        command = _command_text(case.test_command)
        planned_file = case.incumbent.planned_file
        lines.extend(
            [
                f"## Case: {case.case_id}",
                "",
                f"- Comparison class: {case.bakeoff_class}",
                f"- Planned file: {planned_file}",
                f"- Expected changed files: {', '.join(case.incumbent.expected_changed_files)}",
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
                "### Candidate Drop JSON Template",
                "",
                "```json",
                json.dumps(
                    _drop_template(
                        case.case_id,
                        planned_file,
                        command,
                        source_kind,
                        model_name,
                        response_only=response_only,
                    ),
                    indent=2,
                    sort_keys=True,
                ),
                "```",
                "",
            ]
        )
    return "\n".join(lines)


def write_prompt_pack(markdown: str, output_path: Path = DEFAULT_PROMPT_PACK_OUTPUT) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    return output_path


def artifact_summary(artifact: Mapping[str, object], output_path: Path) -> dict[str, object]:
    entries = artifact.get("entries") if isinstance(artifact.get("entries"), list) else []
    source_kinds = sorted(
        {
            str(entry.get("challenger", {}).get("source_kind"))
            for entry in entries
            if isinstance(entry, Mapping) and isinstance(entry.get("challenger"), Mapping)
        }
    )
    return {
        "schema": MODEL_CANDIDATE_ARTIFACT_BUILDER_SCHEMA_VERSION,
        "artifact_schema": artifact.get("schema"),
        "evaluation_mode": artifact.get("evaluation_mode"),
        "cases": len(entries),
        "source_kinds": source_kinds,
        "output": str(output_path),
    }


def prompt_pack_summary(markdown: str, output_path: Path, *, source_kind: str) -> dict[str, object]:
    return {
        "schema": MODEL_CANDIDATE_DROP_PROMPT_PACK_SCHEMA_VERSION,
        "drop_schema": MODEL_CANDIDATE_DROP_SCHEMA_VERSION,
        "provenance_schema": MODEL_CANDIDATE_DROP_PROVENANCE_SCHEMA_VERSION,
        "source_kind": source_kind,
        "cases": len(real_chili_default_cases()),
        "output": str(output_path),
        "bytes": len(markdown.encode("utf-8")),
        "sha256": sha256_text(markdown),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a CHILI model-candidate artifact from collected patch drops."
    )
    parser.add_argument("--drop-dir", type=Path, help="Directory containing candidate-drop JSON files.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--allow-fixture", action="store_true")
    parser.add_argument("--require-provenance", action="store_true")
    parser.add_argument("--prompt-pack", type=Path, help="Prompt pack file to verify by SHA-256.")
    parser.add_argument("--self-test", action="store_true", help="Use synthetic Codex-shaped drops.")
    parser.add_argument("--emit-prompt-pack", action="store_true")
    parser.add_argument("--prompt-pack-output", type=Path, default=DEFAULT_PROMPT_PACK_OUTPUT)
    parser.add_argument("--source-kind", default="codex")
    parser.add_argument("--model-name", default="candidate-model")
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.emit_prompt_pack:
            prompt_pack = render_prompt_pack(
                source_kind=args.source_kind,
                model_name=args.model_name,
            )
            if not args.no_write:
                write_prompt_pack(prompt_pack, args.prompt_pack_output)
            summary = prompt_pack_summary(
                prompt_pack,
                args.prompt_pack_output,
                source_kind=args.source_kind,
            )
            if args.json:
                print(json.dumps(summary, indent=2, sort_keys=True))
            else:
                print(prompt_pack)
                if not args.no_write:
                    print(f"Wrote {args.prompt_pack_output}")
            return 0

        if args.self_test:
            drops = synthetic_drops()
        elif args.drop_dir:
            drops = load_drops(args.drop_dir)
        else:
            raise ArtifactBuildError("--drop-dir is required unless --self-test is used")
        artifact = build_artifact(
            drops,
            allow_partial=args.allow_partial,
            allow_fixture=args.allow_fixture,
            require_provenance=args.require_provenance,
            prompt_pack_path=args.prompt_pack,
        )
        if not args.no_write:
            write_artifact(artifact, args.output)
    except ArtifactBuildError as exc:
        print(f"artifact build error: {exc}", file=sys.stderr)
        return 2

    summary = artifact_summary(artifact, args.output)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(
            "Built model candidate artifact: "
            f"{summary['cases']} cases; source kinds={', '.join(summary['source_kinds']) or 'none'}"
        )
        if not args.no_write:
            print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
