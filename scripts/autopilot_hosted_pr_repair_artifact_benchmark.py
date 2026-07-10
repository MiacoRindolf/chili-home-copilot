from __future__ import annotations

import argparse
import copy
import dataclasses
import hashlib
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.autopilot_frontier_bakeoff_benchmark import _escape_cell  # noqa: E402


DEFAULT_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "HOSTED_PR_REPAIR_ARTIFACT_BENCHMARK.md"
HOSTED_PR_REPAIR_ARTIFACT_SCHEMA_VERSION = "chili.hosted-pr-repair-artifact.v1"
HOSTED_PR_REPAIR_INVENTORY_SCHEMA_VERSION = "chili.hosted-pr-repair-artifact-inventory.v1"
HOSTED_PR_REPAIR_BENCHMARK_SCHEMA_VERSION = "chili.hosted-pr-repair-artifact-benchmark.v1"
TARGET_SCORE = 100
SELF_TEST_EVIDENCE_MODE = "self_test"
REAL_INVENTORY_EVIDENCE_MODE = "real_inventory"
TRANSCRIPT_MIN_EVENTS = 3
SYNTHETIC_MARKERS = ("self-test", "self_test", "synthetic", "fixture", "mock", "deterministic")
REQUIRED_CHECKS = (
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


class HostedPrRepairArtifactError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class HostedPrRepairCheck:
    check_id: str
    expected_status: str
    expected_fragment: str


@dataclasses.dataclass(frozen=True)
class HostedPrRepairResult:
    check: HostedPrRepairCheck
    actual_status: str
    score: int
    evidence: str

    @property
    def passed(self) -> bool:
        return self.score >= TARGET_SCORE


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _as_mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise HostedPrRepairArtifactError(f"{label} must be an object")
    return value


def _required_text(payload: Mapping[str, object], key: str, *, label: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HostedPrRepairArtifactError(f"{label}.{key} is required")
    text = value.strip()
    if _looks_synthetic(text):
        raise HostedPrRepairArtifactError(f"{label}.{key} looks synthetic: {text}")
    return text


def _looks_synthetic(value: object) -> bool:
    text = str(value or "").strip().lower()
    return any(marker in text for marker in SYNTHETIC_MARKERS)


def _resolve_relative_file(raw_path: object, *, base_dir: Path, label: str) -> Path:
    text = _required_text({"path": raw_path}, "path", label=label)
    candidate = Path(text)
    if candidate.is_absolute():
        raise HostedPrRepairArtifactError(f"{label} must be relative")
    resolved_base = base_dir.resolve()
    resolved = (resolved_base / candidate).resolve()
    if resolved_base not in resolved.parents and resolved != resolved_base:
        raise HostedPrRepairArtifactError(f"{label} escapes artifact directory")
    if not resolved.is_file():
        raise HostedPrRepairArtifactError(f"{label} does not exist: {text}")
    return resolved


def _read_json(path: Path) -> Mapping[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise HostedPrRepairArtifactError(f"{path}: invalid JSON: {exc}") from exc
    return _as_mapping(payload, label=str(path))


def _read_transcript_events(path: Path, *, label: str) -> list[Mapping[str, object]]:
    events: list[Mapping[str, object]] = []
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8-sig", errors="replace").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise HostedPrRepairArtifactError(f"{label}:{line_number}: invalid JSONL event: {exc}") from exc
        events.append(_as_mapping(payload, label=f"{label}:{line_number}"))
    if len(events) < TRANSCRIPT_MIN_EVENTS:
        raise HostedPrRepairArtifactError(
            f"{label} must contain at least {TRANSCRIPT_MIN_EVENTS} transcript events"
        )
    return events


def _event_has(events: Sequence[Mapping[str, object]], key: str, expected: str) -> bool:
    return any(str(event.get(key) or "").strip() == expected for event in events)


def _events_text(events: Sequence[Mapping[str, object]]) -> str:
    return "\n".join(json.dumps(dict(event), sort_keys=True) for event in events).lower()


def _validate_transcript_ref(
    ref: Mapping[str, object],
    *,
    base_dir: Path,
    label: str,
) -> tuple[Path, list[Mapping[str, object]]]:
    path = _resolve_relative_file(ref.get("path"), base_dir=base_dir, label=f"{label}.path")
    expected_sha = _required_text(ref, "sha256", label=label).lower()
    if len(expected_sha) != 64 or any(character not in "0123456789abcdef" for character in expected_sha):
        raise HostedPrRepairArtifactError(f"{label}.sha256 must be a SHA-256 hex digest")
    actual_sha = sha256_file(path)
    if actual_sha != expected_sha:
        raise HostedPrRepairArtifactError(f"{label}.sha256 mismatch")
    return path, _read_transcript_events(path, label=label)


def _validate_review_thread_transcript(
    artifact: Mapping[str, object],
    *,
    base_dir: Path,
    label: str,
) -> None:
    ref = _as_mapping(
        artifact.get("review_thread_transcript"),
        label=f"{label}.review_thread_transcript",
    )
    pr_url = _required_text(artifact, "pr_url", label=label)
    thread_id = _required_text(artifact, "review_thread_id", label=label)
    line_thread = _as_mapping(artifact.get("line_thread"), label=f"{label}.line_thread")
    line_thread_id = _required_text(line_thread, "thread_id", label=f"{label}.line_thread")
    if line_thread_id != thread_id:
        raise HostedPrRepairArtifactError(f"{label}.line_thread.thread_id must match review_thread_id")
    if _required_text(ref, "pr_url", label=f"{label}.review_thread_transcript") != pr_url:
        raise HostedPrRepairArtifactError(f"{label}.review_thread_transcript.pr_url mismatch")
    if _required_text(ref, "thread_id", label=f"{label}.review_thread_transcript") != thread_id:
        raise HostedPrRepairArtifactError(f"{label}.review_thread_transcript.thread_id mismatch")
    _required_text(line_thread, "path", label=f"{label}.line_thread")
    _required_text(line_thread, "comment_id", label=f"{label}.line_thread")
    _, events = _validate_transcript_ref(ref, base_dir=base_dir, label=f"{label}.review_thread_transcript")
    if not _event_has(events, "pr_url", pr_url):
        raise HostedPrRepairArtifactError(f"{label}.review_thread_transcript events must include pr_url")
    if not _event_has(events, "thread_id", thread_id):
        raise HostedPrRepairArtifactError(f"{label}.review_thread_transcript events must include thread_id")
    text = _events_text(events)
    if "line" not in text or "comment" not in text:
        raise HostedPrRepairArtifactError(f"{label}.review_thread_transcript must include line-thread detail")


def _validate_publication_transcript(
    artifact: Mapping[str, object],
    *,
    base_dir: Path,
    label: str,
) -> None:
    ref = _as_mapping(
        artifact.get("publication_transcript"),
        label=f"{label}.publication_transcript",
    )
    pr_url = _required_text(artifact, "pr_url", label=label)
    head_sha = _required_text(artifact, "post_repair_head_sha", label=label)
    if _required_text(ref, "pr_url", label=f"{label}.publication_transcript") != pr_url:
        raise HostedPrRepairArtifactError(f"{label}.publication_transcript.pr_url mismatch")
    if _required_text(ref, "commit_sha", label=f"{label}.publication_transcript") != head_sha:
        raise HostedPrRepairArtifactError(f"{label}.publication_transcript.commit_sha mismatch")
    _, events = _validate_transcript_ref(ref, base_dir=base_dir, label=f"{label}.publication_transcript")
    if not _event_has(events, "pr_url", pr_url):
        raise HostedPrRepairArtifactError(f"{label}.publication_transcript events must include pr_url")
    if not _event_has(events, "commit_sha", head_sha):
        raise HostedPrRepairArtifactError(f"{label}.publication_transcript events must include commit_sha")
    text = _events_text(events)
    if "publish" not in text and "push" not in text and "hosted" not in text:
        raise HostedPrRepairArtifactError(f"{label}.publication_transcript must include publication evidence")


def _validate_publication_and_receipt(
    artifact: Mapping[str, object],
    *,
    label: str,
) -> None:
    post_repair_head = _required_text(artifact, "post_repair_head_sha", label=label)
    current_head = _required_text(artifact, "current_head_sha_observed", label=label)
    if post_repair_head != current_head:
        raise HostedPrRepairArtifactError(f"{label}.post_repair_head_sha must match current_head_sha_observed")
    publication = _as_mapping(artifact.get("remote_publication"), label=f"{label}.remote_publication")
    if _required_text(publication, "pr_url", label=f"{label}.remote_publication") != _required_text(
        artifact,
        "pr_url",
        label=label,
    ):
        raise HostedPrRepairArtifactError(f"{label}.remote_publication.pr_url mismatch")
    if _required_text(publication, "commit_sha", label=f"{label}.remote_publication") != post_repair_head:
        raise HostedPrRepairArtifactError(f"{label}.remote_publication.commit_sha mismatch")
    _required_text(publication, "url", label=f"{label}.remote_publication")
    receipt = _as_mapping(
        artifact.get("post_repair_check_receipt"),
        label=f"{label}.post_repair_check_receipt",
    )
    run_id = _required_text(receipt, "run_id", label=f"{label}.post_repair_check_receipt")
    if run_id != _required_text(artifact, "current_hosted_green_run_observed", label=label):
        raise HostedPrRepairArtifactError(f"{label}.post_repair_check_receipt.run_id mismatch")
    if _required_text(receipt, "head_sha", label=f"{label}.post_repair_check_receipt") != current_head:
        raise HostedPrRepairArtifactError(f"{label}.post_repair_check_receipt.head_sha mismatch")
    conclusion = _required_text(receipt, "conclusion", label=f"{label}.post_repair_check_receipt").lower()
    if conclusion != "success":
        raise HostedPrRepairArtifactError(f"{label}.post_repair_check_receipt.conclusion must be success")
    _required_text(receipt, "provider", label=f"{label}.post_repair_check_receipt")


def validate_artifact(artifact: Mapping[str, object], *, base_dir: Path, label: str) -> dict[str, object]:
    schema = artifact.get("schema")
    if schema != HOSTED_PR_REPAIR_ARTIFACT_SCHEMA_VERSION:
        raise HostedPrRepairArtifactError(
            f"{label}.schema is {schema or 'missing'} instead of {HOSTED_PR_REPAIR_ARTIFACT_SCHEMA_VERSION}"
        )
    for key in (
        "pr_url",
        "branch",
        "source_run_id",
        "repair_report",
        "review_thread_id",
        "repaired_head_sha",
        "post_repair_head_sha",
        "current_head_sha_observed",
        "hosted_run_id",
        "current_hosted_green_run_observed",
    ):
        _required_text(artifact, key, label=label)
    _validate_review_thread_transcript(artifact, base_dir=base_dir, label=label)
    _validate_publication_transcript(artifact, base_dir=base_dir, label=label)
    _validate_publication_and_receipt(artifact, label=label)
    return {
        "pr_url": artifact["pr_url"],
        "source_run_id": artifact["source_run_id"],
        "current_head_sha_observed": artifact["current_head_sha_observed"],
        "current_hosted_green_run_observed": artifact["current_hosted_green_run_observed"],
    }


def validate_inventory(inventory: Mapping[str, object], *, base_dir: Path) -> dict[str, object]:
    schema = inventory.get("schema")
    if schema != HOSTED_PR_REPAIR_INVENTORY_SCHEMA_VERSION:
        raise HostedPrRepairArtifactError(
            f"inventory.schema is {schema or 'missing'} instead of {HOSTED_PR_REPAIR_INVENTORY_SCHEMA_VERSION}"
        )
    evidence_mode = inventory.get("evidence_mode")
    if evidence_mode != REAL_INVENTORY_EVIDENCE_MODE:
        raise HostedPrRepairArtifactError("inventory.evidence_mode must be real_inventory")
    artifacts = inventory.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise HostedPrRepairArtifactError("inventory.artifacts must be a non-empty list")
    seen_prs: set[str] = set()
    seen_runs: set[str] = set()
    summaries: list[dict[str, object]] = []
    for index, raw_artifact in enumerate(artifacts, start=1):
        artifact = _as_mapping(raw_artifact, label=f"inventory.artifacts[{index}]")
        summary = validate_artifact(artifact, base_dir=base_dir, label=f"inventory.artifacts[{index}]")
        pr_url = str(summary["pr_url"])
        source_run_id = str(summary["source_run_id"])
        if pr_url in seen_prs:
            raise HostedPrRepairArtifactError(f"duplicate PR artifact: {pr_url}")
        if source_run_id in seen_runs:
            raise HostedPrRepairArtifactError(f"duplicate source_run_id: {source_run_id}")
        seen_prs.add(pr_url)
        seen_runs.add(source_run_id)
        summaries.append(summary)
    return {
        "schema": HOSTED_PR_REPAIR_BENCHMARK_SCHEMA_VERSION,
        "validated_inventory": True,
        "artifacts": len(summaries),
        "prs": sorted(seen_prs),
        "source_runs": sorted(seen_runs),
        "promotion_eligible": True,
    }


def load_inventory(artifact_dir: Path) -> tuple[dict[str, object], Path]:
    inventory_path = artifact_dir
    base_dir = artifact_dir.parent
    if artifact_dir.is_dir():
        inventory_path = artifact_dir / "inventory.json"
        base_dir = artifact_dir
    if not inventory_path.is_file():
        raise HostedPrRepairArtifactError(f"inventory file does not exist: {inventory_path}")
    return dict(_read_json(inventory_path)), base_dir


def _write_jsonl(path: Path, events: Sequence[Mapping[str, object]]) -> None:
    path.write_text(
        "\n".join(json.dumps(dict(event), sort_keys=True) for event in events) + "\n",
        encoding="utf-8",
    )


def _valid_inventory_dir(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    pr_url = "https://github.com/MiacoRindolf/chili-home-copilot/pull/282"
    branch = "codex/stock-momentum-context-gate"
    head_sha = "6160d0f82d749fc04d0f74ea7030d2fd482b3e6d"
    run_id = "26879809423"
    review_thread_id = "PRRT_kwDOExample282"
    review_events = [
        {
            "event": "review_thread_loaded",
            "pr_url": pr_url,
            "thread_id": review_thread_id,
            "path": "app/services/trading/pattern_imminent_alerts.py",
        },
        {
            "event": "line_comment",
            "pr_url": pr_url,
            "thread_id": review_thread_id,
            "comment_id": "PRRC_kwDOExample282",
            "line": 412,
            "body": "The repair must preserve alert context and tests.",
        },
        {
            "event": "repair_plan",
            "pr_url": pr_url,
            "thread_id": review_thread_id,
            "line": 412,
            "resolution": "Added persisted context and focused regression coverage.",
        },
    ]
    publication_events = [
        {
            "event": "publication_started",
            "pr_url": pr_url,
            "commit_sha": head_sha,
            "branch": branch,
        },
        {
            "event": "push_completed",
            "pr_url": pr_url,
            "commit_sha": head_sha,
            "remote": "origin",
        },
        {
            "event": "hosted_check_success_observed",
            "pr_url": pr_url,
            "commit_sha": head_sha,
            "run_id": run_id,
            "conclusion": "success",
        },
    ]
    review_path = root / "review_thread_transcript.jsonl"
    publication_path = root / "publication_transcript.jsonl"
    _write_jsonl(review_path, review_events)
    _write_jsonl(publication_path, publication_events)
    artifact = {
        "schema": HOSTED_PR_REPAIR_ARTIFACT_SCHEMA_VERSION,
        "pr_url": pr_url,
        "branch": branch,
        "source_run_id": "gh-pr282-repair-20260603T1112Z",
        "repair_report": "project_ws/AgentOps/PR_282_CI_REPAIR.md",
        "review_thread_id": review_thread_id,
        "line_thread": {
            "thread_id": review_thread_id,
            "comment_id": "PRRC_kwDOExample282",
            "path": "app/services/trading/pattern_imminent_alerts.py",
            "line": "412",
        },
        "repaired_head_sha": head_sha,
        "post_repair_head_sha": head_sha,
        "current_head_sha_observed": head_sha,
        "hosted_run_id": run_id,
        "current_hosted_green_run_observed": run_id,
        "remote_publication": {
            "url": "https://github.com/MiacoRindolf/chili-home-copilot/actions/runs/26879809423",
            "pr_url": pr_url,
            "commit_sha": head_sha,
        },
        "post_repair_check_receipt": {
            "provider": "github_actions",
            "run_id": run_id,
            "head_sha": head_sha,
            "conclusion": "success",
        },
        "review_thread_transcript": {
            "path": review_path.name,
            "sha256": sha256_file(review_path),
            "pr_url": pr_url,
            "thread_id": review_thread_id,
        },
        "publication_transcript": {
            "path": publication_path.name,
            "sha256": sha256_file(publication_path),
            "pr_url": pr_url,
            "commit_sha": head_sha,
        },
        "failure_context": {
            "kind": "hosted_ci_failure",
            "failing_check": "pytest",
            "failed_run_id": "26877331577",
        },
    }
    inventory = {
        "schema": HOSTED_PR_REPAIR_INVENTORY_SCHEMA_VERSION,
        "generated_utc": "2026-06-03T11:20:00Z",
        "evidence_mode": REAL_INVENTORY_EVIDENCE_MODE,
        "artifacts": [artifact],
    }
    (root / "inventory.json").write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return root


def default_checks() -> list[HostedPrRepairCheck]:
    return [
        HostedPrRepairCheck("valid_hosted_pr_repair_accepts", "accepted", "validated_inventory=True"),
        HostedPrRepairCheck("self_test_artifact_rejected", "rejected", "evidence_mode"),
        HostedPrRepairCheck("missing_review_thread_transcript_rejected", "rejected", "review_thread_transcript"),
        HostedPrRepairCheck("sparse_review_transcript_rejected", "rejected", "at least"),
        HostedPrRepairCheck("review_transcript_pr_mismatch_rejected", "rejected", "review_thread_transcript.pr_url"),
        HostedPrRepairCheck("review_transcript_thread_detail_mismatch_rejected", "rejected", "review_thread_transcript.thread_id"),
        HostedPrRepairCheck("missing_line_thread_rejected", "rejected", "line_thread"),
        HostedPrRepairCheck("missing_remote_publication_rejected", "rejected", "remote_publication"),
        HostedPrRepairCheck("post_repair_head_mismatch_rejected", "rejected", "post_repair_head_sha"),
        HostedPrRepairCheck("missing_post_repair_check_receipt_rejected", "rejected", "post_repair_check_receipt"),
        HostedPrRepairCheck("transcript_hash_mismatch_rejected", "rejected", "sha256 mismatch"),
        HostedPrRepairCheck("sparse_publication_transcript_rejected", "rejected", "at least"),
        HostedPrRepairCheck("publication_transcript_pr_mismatch_rejected", "rejected", "publication_transcript.pr_url"),
        HostedPrRepairCheck("publication_transcript_commit_mismatch_rejected", "rejected", "publication_transcript.commit_sha"),
        HostedPrRepairCheck("valid_artifact_inventory_accepts", "accepted", "artifacts=1"),
        HostedPrRepairCheck("empty_artifact_inventory_rejected", "rejected", "non-empty list"),
        HostedPrRepairCheck("duplicate_pr_artifact_rejected", "rejected", "duplicate PR artifact"),
        HostedPrRepairCheck("duplicate_source_run_rejected", "rejected", "duplicate source_run_id"),
    ]


def _load_valid_inventory(root: Path) -> dict[str, object]:
    inventory_dir = _valid_inventory_dir(root)
    return dict(_read_json(inventory_dir / "inventory.json"))


def _write_sparse_transcript(path: Path, *, event: Mapping[str, object]) -> None:
    _write_jsonl(path, [event])


def _artifact(inventory: Mapping[str, object]) -> dict[str, object]:
    artifacts = inventory["artifacts"]
    if not isinstance(artifacts, list) or not artifacts:
        raise AssertionError("valid inventory helper produced no artifacts")
    return artifacts[0]


def evaluate_check(check: HostedPrRepairCheck) -> HostedPrRepairResult:
    with tempfile.TemporaryDirectory(prefix="chili_hosted_pr_repair_") as raw_root:
        root = Path(raw_root)
        inventory = _load_valid_inventory(root)
        artifact = _artifact(inventory)
        if check.check_id == "self_test_artifact_rejected":
            inventory["evidence_mode"] = SELF_TEST_EVIDENCE_MODE
        elif check.check_id == "missing_review_thread_transcript_rejected":
            artifact.pop("review_thread_transcript", None)
        elif check.check_id == "sparse_review_transcript_rejected":
            review_path = root / str(artifact["review_thread_transcript"]["path"])
            _write_sparse_transcript(
                review_path,
                event={
                    "event": "review_thread_loaded",
                    "pr_url": artifact["pr_url"],
                    "thread_id": artifact["review_thread_id"],
                },
            )
            artifact["review_thread_transcript"]["sha256"] = sha256_file(review_path)
        elif check.check_id == "review_transcript_pr_mismatch_rejected":
            artifact["review_thread_transcript"]["pr_url"] = "https://github.com/MiacoRindolf/chili-home-copilot/pull/999"
        elif check.check_id == "review_transcript_thread_detail_mismatch_rejected":
            artifact["review_thread_transcript"]["thread_id"] = "PRRT_mismatched"
        elif check.check_id == "missing_line_thread_rejected":
            artifact.pop("line_thread", None)
        elif check.check_id == "missing_remote_publication_rejected":
            artifact.pop("remote_publication", None)
        elif check.check_id == "post_repair_head_mismatch_rejected":
            bad_head = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            artifact["post_repair_head_sha"] = bad_head
            publication_path = root / str(artifact["publication_transcript"]["path"])
            _write_jsonl(
                publication_path,
                [
                    {
                        "event": "publication_started",
                        "pr_url": artifact["pr_url"],
                        "commit_sha": bad_head,
                    },
                    {
                        "event": "push_completed",
                        "pr_url": artifact["pr_url"],
                        "commit_sha": bad_head,
                    },
                    {
                        "event": "hosted_check_success_observed",
                        "pr_url": artifact["pr_url"],
                        "commit_sha": bad_head,
                        "run_id": artifact["current_hosted_green_run_observed"],
                    },
                ],
            )
            artifact["publication_transcript"]["commit_sha"] = bad_head
            artifact["publication_transcript"]["sha256"] = sha256_file(publication_path)
        elif check.check_id == "missing_post_repair_check_receipt_rejected":
            artifact.pop("post_repair_check_receipt", None)
        elif check.check_id == "transcript_hash_mismatch_rejected":
            artifact["review_thread_transcript"]["sha256"] = "0" * 64
        elif check.check_id == "sparse_publication_transcript_rejected":
            publication_path = root / str(artifact["publication_transcript"]["path"])
            _write_sparse_transcript(
                publication_path,
                event={
                    "event": "publication_started",
                    "pr_url": artifact["pr_url"],
                    "commit_sha": artifact["post_repair_head_sha"],
                },
            )
            artifact["publication_transcript"]["sha256"] = sha256_file(publication_path)
        elif check.check_id == "publication_transcript_pr_mismatch_rejected":
            artifact["publication_transcript"]["pr_url"] = "https://github.com/MiacoRindolf/chili-home-copilot/pull/999"
        elif check.check_id == "publication_transcript_commit_mismatch_rejected":
            artifact["publication_transcript"]["commit_sha"] = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        elif check.check_id == "empty_artifact_inventory_rejected":
            inventory["artifacts"] = []
        elif check.check_id == "duplicate_pr_artifact_rejected":
            duplicate = copy.deepcopy(artifact)
            duplicate["source_run_id"] = "gh-pr282-repair-duplicate-run"
            inventory["artifacts"] = [artifact, duplicate]
        elif check.check_id == "duplicate_source_run_rejected":
            duplicate = copy.deepcopy(artifact)
            duplicate["pr_url"] = "https://github.com/MiacoRindolf/chili-home-copilot/pull/283"
            duplicate["review_thread_id"] = "PRRT_kwDOExample283"
            duplicate["line_thread"]["thread_id"] = duplicate["review_thread_id"]
            duplicate["line_thread"]["comment_id"] = "PRRC_kwDOExample283"
            review_path = root / "review_thread_transcript_283.jsonl"
            publication_path = root / "publication_transcript_283.jsonl"
            _write_jsonl(
                review_path,
                [
                    {
                        "event": "review_thread_loaded",
                        "pr_url": duplicate["pr_url"],
                        "thread_id": duplicate["review_thread_id"],
                        "path": duplicate["line_thread"]["path"],
                    },
                    {
                        "event": "line_comment",
                        "pr_url": duplicate["pr_url"],
                        "thread_id": duplicate["review_thread_id"],
                        "comment_id": duplicate["line_thread"]["comment_id"],
                        "line": duplicate["line_thread"]["line"],
                        "body": "Second PR repair transcript.",
                    },
                    {
                        "event": "repair_plan",
                        "pr_url": duplicate["pr_url"],
                        "thread_id": duplicate["review_thread_id"],
                        "line": duplicate["line_thread"]["line"],
                        "resolution": "Second PR verified.",
                    },
                ],
            )
            _write_jsonl(
                publication_path,
                [
                    {
                        "event": "publication_started",
                        "pr_url": duplicate["pr_url"],
                        "commit_sha": duplicate["post_repair_head_sha"],
                    },
                    {
                        "event": "push_completed",
                        "pr_url": duplicate["pr_url"],
                        "commit_sha": duplicate["post_repair_head_sha"],
                    },
                    {
                        "event": "hosted_check_success_observed",
                        "pr_url": duplicate["pr_url"],
                        "commit_sha": duplicate["post_repair_head_sha"],
                        "run_id": duplicate["current_hosted_green_run_observed"],
                    },
                ],
            )
            duplicate["review_thread_transcript"]["pr_url"] = duplicate["pr_url"]
            duplicate["review_thread_transcript"]["thread_id"] = duplicate["review_thread_id"]
            duplicate["review_thread_transcript"]["path"] = review_path.name
            duplicate["review_thread_transcript"]["sha256"] = sha256_file(review_path)
            duplicate["publication_transcript"]["pr_url"] = duplicate["pr_url"]
            duplicate["publication_transcript"]["path"] = publication_path.name
            duplicate["publication_transcript"]["sha256"] = sha256_file(publication_path)
            duplicate["remote_publication"]["pr_url"] = duplicate["pr_url"]
            inventory["artifacts"] = [artifact, duplicate]
        try:
            summary = validate_inventory(inventory, base_dir=root)
            actual_status = "accepted"
            evidence = (
                f"validated_inventory={summary['validated_inventory']}; "
                f"artifacts={summary['artifacts']}; "
                f"prs={','.join(str(item) for item in summary['prs'])}"
            )
        except HostedPrRepairArtifactError as exc:
            actual_status = "rejected"
            evidence = str(exc)
    passed = actual_status == check.expected_status and check.expected_fragment in evidence
    return HostedPrRepairResult(
        check=check,
        actual_status=actual_status,
        score=TARGET_SCORE if passed else 0,
        evidence=evidence,
    )


def average_score(results: Sequence[HostedPrRepairResult]) -> int:
    if not results:
        return 0
    return round(sum(result.score for result in results) / len(results))


def missing_checks(results: Sequence[HostedPrRepairResult]) -> list[str]:
    covered = {result.check.check_id for result in results}
    return [check for check in REQUIRED_CHECKS if check not in covered]


def benchmark_status(results: Sequence[HostedPrRepairResult]) -> str:
    if (
        len(results) >= len(REQUIRED_CHECKS)
        and average_score(results) >= TARGET_SCORE
        and all(result.passed for result in results)
        and not missing_checks(results)
    ):
        return "passed"
    return "failed"


def render_scorecard(
    results: Sequence[HostedPrRepairResult],
    *,
    evidence_mode: str = SELF_TEST_EVIDENCE_MODE,
    evidence_summary: Mapping[str, object] | None = None,
    generated_at: datetime | None = None,
) -> str:
    generated_at = generated_at or datetime.now(timezone.utc)
    artifacts: object = "fixture"
    promotion_eligible = False
    if evidence_summary is not None:
        artifacts = evidence_summary.get("artifacts", 0)
        promotion_eligible = bool(evidence_summary.get("promotion_eligible"))
    lines = [
        "# CHILI Hosted PR Repair Artifact Benchmark",
        "",
        f"- Schema: {HOSTED_PR_REPAIR_BENCHMARK_SCHEMA_VERSION}",
        f"- Generated UTC: {generated_at.isoformat().replace('+00:00', 'Z')}",
        f"- Status: {benchmark_status(results)}",
        f"- Target score: {TARGET_SCORE}",
        f"- Evidence mode: {evidence_mode}",
        f"- Checks: {len(results)}",
        f"- Average score: {average_score(results)}/100",
        f"- Artifacts: {artifacts}",
        f"- Promotion eligible: {str(promotion_eligible).lower()}",
        f"- Required checks: {', '.join(REQUIRED_CHECKS)}",
        f"- Missing checks: {', '.join(missing_checks(results)) or 'none'}",
        "- Required behavior: hosted PR repair promotion must be backed by transcript-bound review, publication, current-head, and hosted green-check receipts.",
        "- Safety: local artifact/hash validation only; no git action, PR mutation, runtime restart, deployment, database migration, broker call, or live-trading action.",
        "",
        "| Check | Expected | Actual | Score | Evidence |",
        "| --- | --- | --- | ---: | --- |",
    ]
    for result in results:
        lines.append(
            "| "
            + " | ".join(
                [
                    _escape_cell(result.check.check_id),
                    _escape_cell(result.check.expected_status),
                    _escape_cell(result.actual_status),
                    str(result.score),
                    _escape_cell(result.evidence),
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def write_scorecard(markdown: str, output_path: Path = DEFAULT_OUTPUT) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    return output_path


def run_hosted_pr_repair_benchmark(
    *,
    output_path: Path = DEFAULT_OUTPUT,
    write: bool = True,
) -> tuple[list[HostedPrRepairResult], str, Path]:
    results = [evaluate_check(check) for check in default_checks()]
    markdown = render_scorecard(results, evidence_mode=SELF_TEST_EVIDENCE_MODE)
    if write:
        write_scorecard(markdown, output_path)
    return results, markdown, output_path


def run_hosted_pr_repair_validation(
    inventory: Mapping[str, object],
    *,
    base_dir: Path,
    output_path: Path = DEFAULT_OUTPUT,
    write: bool = True,
) -> tuple[list[HostedPrRepairResult], str, Path, dict[str, object]]:
    summary = validate_inventory(inventory, base_dir=base_dir)
    results = [evaluate_check(check) for check in default_checks()]
    markdown = render_scorecard(
        results,
        evidence_mode=REAL_INVENTORY_EVIDENCE_MODE,
        evidence_summary=summary,
    )
    if write:
        write_scorecard(markdown, output_path)
    return results, markdown, output_path, summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate transcript-bound hosted PR repair artifacts or replay the artifact gate."
    )
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.inventory and args.artifact_dir:
            raise HostedPrRepairArtifactError("--inventory and --artifact-dir cannot both be used")
        if args.inventory or args.artifact_dir:
            inventory, base_dir = load_inventory(args.inventory or args.artifact_dir)
            results, markdown, output_path, evidence_summary = run_hosted_pr_repair_validation(
                inventory,
                base_dir=base_dir,
                output_path=args.output,
                write=not args.no_write,
            )
            evidence_mode = REAL_INVENTORY_EVIDENCE_MODE
        else:
            results, markdown, output_path = run_hosted_pr_repair_benchmark(
                output_path=args.output,
                write=not args.no_write,
            )
            evidence_summary = {
                "validated_inventory": False,
                "artifacts": "fixture",
                "promotion_eligible": False,
            }
            evidence_mode = SELF_TEST_EVIDENCE_MODE
    except HostedPrRepairArtifactError as exc:
        print(f"hosted PR repair artifact error: {exc}", file=sys.stderr)
        return 2

    status = benchmark_status(results)
    if args.json:
        print(
            json.dumps(
                {
                    "schema": HOSTED_PR_REPAIR_BENCHMARK_SCHEMA_VERSION,
                    "status": status,
                    "evidence_mode": evidence_mode,
                    "average_score": average_score(results),
                    "checks": len(results),
                    "missing_checks": missing_checks(results),
                    "promotion_eligible": bool(evidence_summary.get("promotion_eligible")),
                    "artifacts": evidence_summary.get("artifacts"),
                    "output": str(output_path),
                    "written": not args.no_write,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(markdown)
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
