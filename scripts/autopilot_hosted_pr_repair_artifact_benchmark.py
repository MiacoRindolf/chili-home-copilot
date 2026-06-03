from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import re
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
HOSTED_PR_REPAIR_BENCHMARK_SCHEMA_VERSION = "chili.hosted-pr-repair-artifact-benchmark.v1"
TARGET_SCORE = 100
SELF_TEST_EVIDENCE_MODE = "self_test"
REAL_INVENTORY_EVIDENCE_MODE = "real_inventory"
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
SYNTHETIC_MARKERS = ("self-test", "self_test", "synthetic", "fixture", "mock", "deterministic")
TRANSCRIPT_MIN_EVENTS = 3


class HostedPrRepairEvidenceError(ValueError):
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
        raise HostedPrRepairEvidenceError(f"{label} must be an object")
    return value


def _required_text(payload: Mapping[str, object], key: str, *, label: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HostedPrRepairEvidenceError(f"{label}.{key} is required")
    return value.strip()


def _looks_synthetic(value: object) -> bool:
    text = str(value or "").strip().lower()
    return any(marker in text for marker in SYNTHETIC_MARKERS)


def _parse_utc_timestamp(value: object, *, label: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise HostedPrRepairEvidenceError(f"{label} is required")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HostedPrRepairEvidenceError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    if parsed > now.replace(microsecond=now.microsecond) and (parsed - now).total_seconds() > 600:
        raise HostedPrRepairEvidenceError(f"{label} is in the future")
    return parsed


def _safe_relative_file(base_dir: Path, raw_path: object, *, field_name: str) -> Path:
    text = _required_text({"path": raw_path}, "path", label=field_name)
    candidate = Path(text)
    if candidate.is_absolute():
        raise HostedPrRepairEvidenceError(f"{field_name} must be relative to the artifact JSON")
    resolved_base = base_dir.resolve()
    resolved = (resolved_base / candidate).resolve()
    if resolved_base not in resolved.parents and resolved != resolved_base:
        raise HostedPrRepairEvidenceError(f"{field_name} escapes artifact directory")
    if not resolved.is_file():
        raise HostedPrRepairEvidenceError(f"{field_name} does not exist: {text}")
    return resolved


def _verify_transcript(
    artifact: Mapping[str, object],
    *,
    base_dir: Path,
    file_key: str,
    hash_key: str,
    label: str,
) -> Path:
    evidence = _as_mapping(artifact.get("evidence"), label="artifact.evidence")
    path = _safe_relative_file(base_dir, evidence.get(file_key), field_name=f"evidence.{file_key}")
    expected = _required_text(evidence, hash_key, label="artifact.evidence")
    actual = sha256_file(path)
    if actual != expected:
        raise HostedPrRepairEvidenceError(f"{label} sha256 mismatch")
    return path


def _verify_optional_transcript(
    artifact: Mapping[str, object],
    *,
    base_dir: Path,
    file_key: str,
    hash_key: str,
    label: str,
) -> Path | None:
    evidence = _as_mapping(artifact.get("evidence"), label="artifact.evidence")
    if not evidence.get(file_key) and not evidence.get(hash_key):
        return None
    return _verify_transcript(
        artifact,
        base_dir=base_dir,
        file_key=file_key,
        hash_key=hash_key,
        label=label,
    )


def _read_transcript_lines(path: Path, *, label: str) -> list[str]:
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        raise HostedPrRepairEvidenceError(f"{label} must contain at least {TRANSCRIPT_MIN_EVENTS} non-empty events")
    return lines


def _require_transcript_quality(
    path: Path,
    *,
    label: str,
    required_fragments: Sequence[str],
    min_events: int = TRANSCRIPT_MIN_EVENTS,
) -> int:
    lines = _read_transcript_lines(path, label=label)
    if len(lines) < min_events:
        raise HostedPrRepairEvidenceError(f"{label} must contain at least {min_events} non-empty events")
    text = "\n".join(lines).lower()
    for fragment in required_fragments:
        if fragment.lower() not in text:
            raise HostedPrRepairEvidenceError(f"{label} must include {fragment} evidence")
    return len(lines)


def _read_transcript_events(path: Path, *, label: str) -> list[Mapping[str, object]]:
    events: list[Mapping[str, object]] = []
    for index, line in enumerate(_read_transcript_lines(path, label=label), start=1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise HostedPrRepairEvidenceError(f"{label} event {index} must be valid JSON") from exc
        if not isinstance(event, Mapping):
            raise HostedPrRepairEvidenceError(f"{label} event {index} must be an object")
        events.append(event)
    return events


def _event_text(event: Mapping[str, object], key: str) -> str:
    return str(event.get(key) or "").strip()


def _event_commit(event: Mapping[str, object]) -> str:
    return str(
        event.get("commit_sha")
        or event.get("head_sha")
        or event.get("head_ref_oid")
        or event.get("sha")
        or ""
    ).strip()


def _require_review_transcript_binding(
    path: Path,
    *,
    label: str,
    pr_url: str,
    line_threads: Sequence[Mapping[str, object]],
) -> None:
    events = _read_transcript_events(path, label=label)
    if not any(_event_text(event, "pr_url") == pr_url for event in events):
        raise HostedPrRepairEvidenceError(f"{label} must include {pr_url} evidence")
    for item in line_threads:
        thread_path = str(item.get("path") or "").strip()
        thread_line = str(item.get("line") or "").strip()
        thread_body = str(item.get("body") or "").strip()
        if not any(
            _event_text(event, "path") == thread_path
            and _event_text(event, "line") == thread_line
            and _event_text(event, "body") == thread_body
            for event in events
        ):
            raise HostedPrRepairEvidenceError(
                f"{label} must include unresolved review thread {thread_path}:{thread_line} evidence"
            )


def _require_publication_transcript_binding(
    path: Path,
    *,
    label: str,
    pr_url: str,
    final_commit: str,
) -> None:
    events = _read_transcript_events(path, label=label)
    if not any(_event_text(event, "pr_url") == pr_url for event in events):
        raise HostedPrRepairEvidenceError(f"{label} must include {pr_url} evidence")
    if not any(
        _event_text(event, "pr_url") == pr_url
        and (
            _event_text(event, "publication_state") == "pr_published"
            or _event_text(event, "event") == "pr_published"
        )
        and _event_commit(event).lower() == final_commit.lower()
        for event in events
    ):
        raise HostedPrRepairEvidenceError(f"{label} must include pr_published evidence for repaired commit")
    if not any(_check_receipt_is_current_head_success(event, final_commit=final_commit) for event in events):
        raise HostedPrRepairEvidenceError(f"{label} must include current-head successful check evidence")


def _require_failure_transcript_binding(
    path: Path,
    *,
    label: str,
    pr_url: str,
    before_commit: str,
    failed_checks: Sequence[Mapping[str, object]],
) -> None:
    events = _read_transcript_events(path, label=label)
    if not any(_event_text(event, "pr_url") == pr_url for event in events):
        raise HostedPrRepairEvidenceError(f"{label} must include {pr_url} evidence")
    if before_commit and not any(_event_commit(event).lower() == before_commit.lower() for event in events):
        raise HostedPrRepairEvidenceError(f"{label} must include failing head {before_commit} evidence")
    for receipt in failed_checks[:3]:
        name = str(receipt.get("name") or receipt.get("check_name") or receipt.get("workflow") or "").strip()
        if not name:
            continue
        if not any(
            _event_text(event, "name") == name
            or _event_text(event, "check_name") == name
            or _event_text(event, "workflow") == name
            for event in events
        ):
            raise HostedPrRepairEvidenceError(f"{label} must include failing check {name} evidence")


def _valid_hosted_pr_url(value: object) -> str:
    pr_url = _required_text({"pr_url": value}, "pr_url", label="artifact")
    if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*", pr_url):
        raise HostedPrRepairEvidenceError("artifact.pr_url must be a hosted GitHub pull request URL")
    return pr_url


def _line_thread_items(status: Mapping[str, object]) -> list[Mapping[str, object]]:
    feedback = _as_mapping(status.get("review_feedback"), label="pr_status_before.review_feedback")
    items = feedback.get("items")
    if not isinstance(items, list):
        raise HostedPrRepairEvidenceError("review_feedback.items must be a list")
    return [
        item
        for item in items
        if isinstance(item, Mapping)
        and item.get("source") == "review_thread"
        and item.get("resolved") is not True
        and str(item.get("path") or "")
        and str(item.get("line") or "")
        and str(item.get("body") or "")
    ]


def _check_receipt_rows(status: Mapping[str, object]) -> list[object]:
    rows: list[object] = []
    for key in (
        "current_head_check_receipts",
        "successful_checks",
        "passing_checks",
        "completed_checks",
        "check_runs",
        "checks",
        "check_receipts",
    ):
        value = status.get(key)
        if isinstance(value, Mapping):
            rows.append(value)
        elif isinstance(value, list):
            rows.extend(value)
    return rows[:20]


def _failed_check_receipt_rows(status: Mapping[str, object]) -> list[object]:
    rows: list[object] = []
    for key in (
        "current_head_check_receipts",
        "failed_checks",
        "failing_checks",
        "check_runs",
        "checks",
        "check_receipts",
        "status_check_rollup",
        "statusCheckRollup",
    ):
        value = status.get(key)
        if isinstance(value, list):
            rows.extend(value)
        elif isinstance(value, Mapping):
            rows.append(value)
    return rows[:30]


def _check_receipt_is_current_head_failure(row: object, *, before_commit: str) -> bool:
    if isinstance(row, Mapping):
        conclusion = str(row.get("conclusion") or row.get("result") or row.get("state") or "").strip().lower()
        status = str(row.get("status") or "").strip().lower()
        text = " ".join(
            str(row.get(key) or "")
            for key in ("name", "summary", "conclusion", "result", "state")
        ).lower()
        failed = (
            conclusion in {"failure", "failed", "error", "timed_out", "cancelled", "action_required"}
            or "failure" in text
            or "failed" in text
        ) and status not in {"queued", "in_progress", "pending", "waiting", "requested"}
        if not failed:
            return False
        receipt_head = str(
            row.get("head_sha")
            or row.get("head_ref_oid")
            or row.get("commit_sha")
            or row.get("sha")
            or ""
        ).strip()
        if before_commit and receipt_head and receipt_head.lower() != before_commit.lower():
            return False
        has_name = bool(str(row.get("name") or row.get("check_name") or row.get("workflow") or "").strip())
        has_provenance = any(
            str(row.get(key) or "").strip()
            for key in (
                "url",
                "html_url",
                "details_url",
                "started_at",
                "completed_at",
                "timestamp",
                "timestamp_utc",
                "source",
                "run_id",
                "check_run_id",
            )
        )
        return bool((has_name or receipt_head) and has_provenance)
    text = str(row or "").strip()
    return bool(
        text
        and re.search(r"\b(failure|failed|error|timed[_ -]?out)\b", text, re.IGNORECASE)
        and re.search(r"\b(url|https?://|completed_at|timestamp|source|run[_ -]?id|sha)\b", text, re.IGNORECASE)
    )


def _pre_repair_failed_check_receipts(
    before: Mapping[str, object],
    *,
    before_commit: str,
) -> list[Mapping[str, object]]:
    return [
        row
        for row in _failed_check_receipt_rows(before)
        if isinstance(row, Mapping)
        and _check_receipt_is_current_head_failure(row, before_commit=before_commit)
    ]


def _check_receipt_is_current_head_success(row: object, *, final_commit: str) -> bool:
    if isinstance(row, Mapping):
        conclusion = str(row.get("conclusion") or row.get("result") or row.get("state") or "").strip().lower()
        status = str(row.get("status") or "").strip().lower()
        text = " ".join(str(row.get(key) or "") for key in ("name", "summary", "conclusion", "result", "state")).lower()
        success = (
            conclusion in {"success", "successful", "passed", "pass", "green"}
            or "success" in text
            or "passed" in text
        ) and status not in {"queued", "in_progress", "pending", "waiting", "requested"}
        if not success:
            return False
        receipt_head = str(
            row.get("head_sha")
            or row.get("head_ref_oid")
            or row.get("commit_sha")
            or row.get("sha")
            or ""
        ).strip()
        if receipt_head and receipt_head.lower() != final_commit.lower():
            return False
        has_name = bool(str(row.get("name") or row.get("check_name") or row.get("workflow") or "").strip())
        has_provenance = any(
            str(row.get(key) or "").strip()
            for key in (
                "url",
                "html_url",
                "details_url",
                "started_at",
                "completed_at",
                "timestamp",
                "timestamp_utc",
                "source",
                "run_id",
                "check_run_id",
            )
        )
        return bool((has_name or receipt_head) and has_provenance)
    text = str(row or "").strip()
    return bool(
        text
        and re.search(r"\b(success|passed|green)\b", text, re.IGNORECASE)
        and re.search(r"\b(url|https?://|completed_at|timestamp|source|run[_ -]?id|sha)\b", text, re.IGNORECASE)
    )


def _require_post_repair_check_receipt(post: Mapping[str, object], *, final_commit: str) -> int:
    receipts = [
        row
        for row in _check_receipt_rows(post)
        if _check_receipt_is_current_head_success(row, final_commit=final_commit)
    ]
    if not receipts:
        raise HostedPrRepairEvidenceError(
            "post_repair_status requires a current-head successful check receipt with name, status, source/timestamp/url, and matching head when present"
        )
    return len(receipts)


def validate_hosted_pr_repair_artifact(
    artifact: Mapping[str, object],
    *,
    base_dir: Path | None = None,
) -> dict[str, object]:
    base_dir = base_dir or Path(str(artifact.get("_artifact_dir") or "."))
    schema = artifact.get("schema")
    if schema != HOSTED_PR_REPAIR_ARTIFACT_SCHEMA_VERSION:
        raise HostedPrRepairEvidenceError(
            f"artifact.schema is {schema or 'missing'} instead of {HOSTED_PR_REPAIR_ARTIFACT_SCHEMA_VERSION}"
        )
    if artifact.get("hosted") is not True:
        raise HostedPrRepairEvidenceError("artifact.hosted must be true")
    pr_url = _valid_hosted_pr_url(artifact.get("pr_url"))
    collected_at = _parse_utc_timestamp(artifact.get("collected_at"), label="artifact.collected_at")
    for key in ("artifact_id", "source_run_id", "source_agent"):
        text = _required_text(artifact, key, label="artifact")
        if _looks_synthetic(text):
            raise HostedPrRepairEvidenceError(f"artifact.{key} looks synthetic: {text}")

    review_transcript = _verify_optional_transcript(
        artifact,
        base_dir=base_dir,
        file_key="review_thread_transcript_file",
        hash_key="review_thread_transcript_sha256",
        label="review thread transcript",
    )
    failure_transcript = _verify_optional_transcript(
        artifact,
        base_dir=base_dir,
        file_key="failure_transcript_file",
        hash_key="failure_transcript_sha256",
        label="failure transcript",
    )
    publication_transcript = _verify_transcript(
        artifact,
        base_dir=base_dir,
        file_key="publication_transcript_file",
        hash_key="publication_transcript_sha256",
        label="publication transcript",
    )

    repair = _as_mapping(artifact.get("repair_result"), label="artifact.repair_result")
    if repair.get("status") != "success":
        raise HostedPrRepairEvidenceError("repair_result.status must be success")
    final_commit = _required_text(repair, "final_commit_sha", label="artifact.repair_result")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", final_commit):
        raise HostedPrRepairEvidenceError("repair_result.final_commit_sha must be a full git SHA")
    handoff = _as_mapping(repair.get("handoff"), label="artifact.repair_result.handoff")
    if handoff.get("publication_state") != "repair_not_published":
        raise HostedPrRepairEvidenceError("repair handoff must be repair_not_published before operator publication")
    monitoring = _as_mapping(handoff.get("post_repair_monitoring"), label="repair handoff post_repair_monitoring")
    if monitoring.get("required_after_publication") is not True:
        raise HostedPrRepairEvidenceError("post_repair_monitoring.required_after_publication must be true")

    before = _as_mapping(artifact.get("pr_status_before"), label="artifact.pr_status_before")
    if before.get("ok") is not True or before.get("merge_ready") is not False:
        raise HostedPrRepairEvidenceError("pr_status_before must be ok and not merge-ready")
    if before.get("pr_url") != pr_url:
        raise HostedPrRepairEvidenceError("pr_status_before.pr_url must match artifact.pr_url")
    before_commit = str(
        before.get("commit_sha")
        or before.get("head_sha")
        or before.get("head_ref_oid")
        or ""
    ).strip()
    line_threads = _line_thread_items(before)
    failed_checks = _pre_repair_failed_check_receipts(before, before_commit=before_commit)
    review_transcript_events = 0
    failure_transcript_events = 0
    repair_evidence_mode = ""
    if line_threads:
        ingestion = _as_mapping(
            before.get("review_thread_ingestion"),
            label="pr_status_before.review_thread_ingestion",
        )
        if ingestion.get("ok") is not True or int(ingestion.get("total") or 0) < 1:
            raise HostedPrRepairEvidenceError("review thread ingestion must prove at least one hosted thread")
        if review_transcript is None:
            raise HostedPrRepairEvidenceError(
                "review thread transcript is required for review-thread repair evidence"
            )
        review_transcript_events = _require_transcript_quality(
            review_transcript,
            label="review thread transcript",
            required_fragments=("review_thread", pr_url),
        )
        _require_review_transcript_binding(
            review_transcript,
            label="review thread transcript",
            pr_url=pr_url,
            line_threads=line_threads,
        )
        repair_evidence_mode = "review_thread"
    elif failed_checks:
        if failure_transcript is None:
            raise HostedPrRepairEvidenceError(
                "failure transcript is required for hosted CI failure repair evidence"
            )
        failure_transcript_events = _require_transcript_quality(
            failure_transcript,
            label="failure transcript",
            required_fragments=("failure", pr_url),
        )
        _require_failure_transcript_binding(
            failure_transcript,
            label="failure transcript",
            pr_url=pr_url,
            before_commit=before_commit,
            failed_checks=failed_checks,
        )
        repair_evidence_mode = "hosted_ci_failure"
    else:
        raise HostedPrRepairEvidenceError(
            "at least one unresolved line-level review thread or hosted failing check receipt is required"
        )

    publication = _as_mapping(artifact.get("publication_result"), label="artifact.publication_result")
    if publication.get("publication_state") != "pr_published":
        raise HostedPrRepairEvidenceError("publication_result.publication_state must be pr_published")
    if publication.get("commit_sha") != final_commit:
        raise HostedPrRepairEvidenceError("publication commit must match repaired commit")
    post = _as_mapping(publication.get("post_repair_status"), label="publication_result.post_repair_status")
    if post.get("ok") is not True or post.get("merge_ready") is not True:
        raise HostedPrRepairEvidenceError("post_repair_status must be ok and merge-ready")
    if post.get("commit_sha") != final_commit:
        raise HostedPrRepairEvidenceError("post_repair_status commit must match repaired commit")
    if post.get("pr_url") != pr_url:
        raise HostedPrRepairEvidenceError("post_repair_status.pr_url must match artifact.pr_url")
    checks = _as_mapping(post.get("checks"), label="post_repair_status.checks")
    if int(checks.get("failed") or 0) != 0 or int(checks.get("pending") or 0) != 0:
        raise HostedPrRepairEvidenceError("post_repair_status must have zero failed and pending checks")
    post_repair_check_receipts = _require_post_repair_check_receipt(post, final_commit=final_commit)
    publication_transcript_events = _require_transcript_quality(
        publication_transcript,
        label="publication transcript",
        required_fragments=("pr_published", "check", "success", pr_url, final_commit),
    )
    _require_publication_transcript_binding(
        publication_transcript,
        label="publication transcript",
        pr_url=pr_url,
        final_commit=final_commit,
    )

    return {
        "schema": HOSTED_PR_REPAIR_BENCHMARK_SCHEMA_VERSION,
        "validated_hosted_pr_repair_artifact": True,
        "pr_url": pr_url,
        "repair_evidence_mode": repair_evidence_mode,
        "line_threads": len(line_threads),
        "pre_repair_failed_check_receipts": len(failed_checks),
        "review_thread_transcript": review_transcript.name if review_transcript else "",
        "review_thread_transcript_events": review_transcript_events,
        "failure_transcript": failure_transcript.name if failure_transcript else "",
        "failure_transcript_events": failure_transcript_events,
        "publication_transcript": publication_transcript.name,
        "publication_transcript_events": publication_transcript_events,
        "final_commit_sha": final_commit,
        "post_repair_merge_ready": True,
        "post_repair_check_receipts": post_repair_check_receipts,
        "source_agent": _required_text(artifact, "source_agent", label="artifact"),
        "source_run_id": _required_text(artifact, "source_run_id", label="artifact"),
        "artifact_id": _required_text(artifact, "artifact_id", label="artifact"),
        "collected_at": collected_at.isoformat().replace("+00:00", "Z"),
    }


def default_checks() -> list[HostedPrRepairCheck]:
    return [
        HostedPrRepairCheck("valid_hosted_pr_repair_accepts", "accepted", "validated_hosted_pr_repair_artifact=True"),
        HostedPrRepairCheck("self_test_artifact_rejected", "rejected", "looks synthetic"),
        HostedPrRepairCheck("missing_review_thread_transcript_rejected", "rejected", "review_thread_transcript_file does not exist"),
        HostedPrRepairCheck("sparse_review_transcript_rejected", "rejected", "review thread transcript must contain at least 3 non-empty events"),
        HostedPrRepairCheck("review_transcript_pr_mismatch_rejected", "rejected", "review thread transcript must include https://github.com/acme/chili/pull/4242 evidence"),
        HostedPrRepairCheck("review_transcript_thread_detail_mismatch_rejected", "rejected", "review thread transcript must include unresolved review thread app/main.py:7 evidence"),
        HostedPrRepairCheck("missing_line_thread_rejected", "rejected", "unresolved line-level review thread"),
        HostedPrRepairCheck("missing_remote_publication_rejected", "rejected", "publication_state must be pr_published"),
        HostedPrRepairCheck("post_repair_head_mismatch_rejected", "rejected", "post_repair_status commit must match"),
        HostedPrRepairCheck("missing_post_repair_check_receipt_rejected", "rejected", "current-head successful check receipt"),
        HostedPrRepairCheck("transcript_hash_mismatch_rejected", "rejected", "sha256 mismatch"),
        HostedPrRepairCheck("sparse_publication_transcript_rejected", "rejected", "publication transcript must contain at least 3 non-empty events"),
        HostedPrRepairCheck("publication_transcript_pr_mismatch_rejected", "rejected", "publication transcript must include https://github.com/acme/chili/pull/4242 evidence"),
        HostedPrRepairCheck("publication_transcript_commit_mismatch_rejected", "rejected", "publication transcript must include pr_published evidence for repaired commit"),
        HostedPrRepairCheck("valid_artifact_inventory_accepts", "accepted", "validated_hosted_pr_repair_artifact_inventory=True"),
        HostedPrRepairCheck("empty_artifact_inventory_rejected", "rejected", "at least 1 hosted PR repair artifact"),
        HostedPrRepairCheck("duplicate_pr_artifact_rejected", "rejected", "duplicate pr_url"),
        HostedPrRepairCheck("duplicate_source_run_rejected", "rejected", "duplicate source_run_id"),
    ]


def _valid_artifact_context(
    root: Path,
    *,
    pr_number: int = 4242,
    final_commit: str | None = None,
    artifact_id: str | None = None,
    source_run_id: str | None = None,
    source_agent: str = "codex",
) -> tuple[dict[str, object], Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    review_transcript = root / "review-thread.transcript.jsonl"
    publication_transcript = root / "publication.transcript.jsonl"
    pr_url = f"https://github.com/acme/chili/pull/{pr_number}"
    final_commit = final_commit or ("a" * 40)
    artifact_id = artifact_id or f"hosted-pr-repair-20260602T145000Z-{pr_number}"
    source_run_id = source_run_id or f"codex-hosted-pr-repair-20260602T145000Z-{pr_number}"
    review_transcript.write_text(
        "\n".join(
            json.dumps(event, sort_keys=True)
            for event in (
                {
                    "event": "review_thread_captured",
                    "pr_url": pr_url,
                    "thread_id": "PRRT_kwDOHostedThread",
                    "path": "app/main.py",
                    "line": 7,
                },
                {
                    "event": "review_thread_comment",
                    "thread_id": "PRRT_kwDOHostedThread",
                    "author": "reviewer",
                    "path": "app/main.py",
                    "line": 7,
                    "body": "The hosted PR still returns the stale value here.",
                },
                {
                    "event": "review_thread_ingested",
                    "source": "github_review_threads",
                    "total": 1,
                    "unresolved": 1,
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )
    publication_transcript.write_text(
        "\n".join(
            json.dumps(event, sort_keys=True)
            for event in (
                {
                    "event": "operator_approved_repaired_publication",
                    "pr_url": pr_url,
                    "commit_sha": final_commit,
                    "post_repair_merge_ready": True,
                },
                {
                    "event": "pr_published",
                    "publication_state": "pr_published",
                    "pr_url": pr_url,
                    "commit_sha": final_commit,
                },
                {
                    "event": "post_repair_check_receipt",
                    "name": "test",
                    "conclusion": "success",
                    "head_sha": final_commit,
                    "url": f"https://github.com/acme/chili/actions/runs/{pr_number}",
                    "completed_at": "2026-06-02T14:59:00Z",
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )
    artifact = {
        "schema": HOSTED_PR_REPAIR_ARTIFACT_SCHEMA_VERSION,
        "artifact_id": artifact_id,
        "hosted": True,
        "source_agent": source_agent,
        "source_run_id": source_run_id,
        "collected_at": _utc_now(),
        "pr_url": pr_url,
        "evidence": {
            "review_thread_transcript_file": review_transcript.name,
            "review_thread_transcript_sha256": sha256_file(review_transcript),
            "publication_transcript_file": publication_transcript.name,
            "publication_transcript_sha256": sha256_file(publication_transcript),
        },
        "pr_status_before": {
            "ok": True,
            "merge_ready": False,
            "pr_url": pr_url,
            "blockers": ["checks_failed", "review_not_approved"],
            "review_thread_ingestion": {"ok": True, "total": 1},
            "review_feedback": {
                "total": 1,
                "items": [
                    {
                        "source": "review_thread",
                        "author": "reviewer",
                        "path": "app/main.py",
                        "line": "7",
                        "resolved": False,
                        "outdated": False,
                        "diff_hunk": "@@ -7 +7 @@ stale value",
                        "body": "The hosted PR still returns the stale value here.",
                    }
                ],
            },
        },
        "repair_result": {
            "status": "success",
            "final_commit_sha": final_commit,
            "final_files_changed": ["app/main.py"],
            "handoff": {
                "publication_state": "repair_not_published",
                "post_repair_monitoring": {
                    "required_after_publication": True,
                    "pr_identifier": "4242",
                    "expected_branch": "chili/auto/hosted-pr-repair",
                },
            },
        },
        "publication_result": {
            "publication_state": "pr_published",
            "commit_sha": final_commit,
            "pr_output": pr_url,
            "post_repair_status": {
                "ok": True,
                "merge_ready": True,
                "pr_url": pr_url,
                "commit_sha": final_commit,
                "checks": {"total": 3, "passed": 3, "failed": 0, "pending": 0},
                "successful_checks": [
                    {
                        "name": "test",
                        "status": "COMPLETED",
                        "conclusion": "SUCCESS",
                        "head_sha": final_commit,
                        "url": f"https://github.com/acme/chili/actions/runs/{pr_number}",
                        "completed_at": "2026-06-02T14:59:00Z",
                    }
                ],
            },
        },
    }
    return artifact, review_transcript, publication_transcript


def _valid_ci_failure_artifact_context(
    root: Path,
    *,
    pr_number: int = 282,
    before_commit: str | None = None,
    final_commit: str | None = None,
) -> tuple[dict[str, object], Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    failure_transcript = root / "failure.transcript.jsonl"
    publication_transcript = root / "publication.transcript.jsonl"
    pr_url = f"https://github.com/acme/chili/pull/{pr_number}"
    before_commit = before_commit or ("6" * 40)
    final_commit = final_commit or ("7" * 40)
    failure_transcript.write_text(
        "\n".join(
            json.dumps(event, sort_keys=True)
            for event in (
                {
                    "event": "hosted_ci_failure_captured",
                    "pr_url": pr_url,
                    "commit_sha": before_commit,
                    "name": "test",
                    "conclusion": "failure",
                    "url": f"https://github.com/acme/chili/actions/runs/{pr_number}",
                },
                {
                    "event": "failed_check_log",
                    "pr_url": pr_url,
                    "head_sha": before_commit,
                    "name": "test",
                    "failure": "pytest failed in tests/test_canonical_outcome_layer.py",
                },
                {
                    "event": "failure_ingested",
                    "pr_url": pr_url,
                    "head_sha": before_commit,
                    "source": "github_check_run",
                    "failed_checks": 1,
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )
    publication_transcript.write_text(
        "\n".join(
            json.dumps(event, sort_keys=True)
            for event in (
                {
                    "event": "operator_approved_repaired_publication",
                    "pr_url": pr_url,
                    "commit_sha": final_commit,
                    "post_repair_merge_ready": True,
                },
                {
                    "event": "pr_published",
                    "publication_state": "pr_published",
                    "pr_url": pr_url,
                    "commit_sha": final_commit,
                },
                {
                    "event": "post_repair_check_receipt",
                    "name": "test",
                    "conclusion": "success",
                    "head_sha": final_commit,
                    "url": f"https://github.com/acme/chili/actions/runs/{pr_number + 1}",
                    "completed_at": "2026-06-03T11:06:58Z",
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )
    artifact = {
        "schema": HOSTED_PR_REPAIR_ARTIFACT_SCHEMA_VERSION,
        "artifact_id": f"hosted-pr-repair-ci-{pr_number}-20260603T111200Z",
        "hosted": True,
        "source_agent": "codex",
        "source_run_id": f"codex-hosted-ci-repair-20260603T111200Z-{pr_number}",
        "collected_at": _utc_now(),
        "pr_url": pr_url,
        "evidence": {
            "failure_transcript_file": failure_transcript.name,
            "failure_transcript_sha256": sha256_file(failure_transcript),
            "publication_transcript_file": publication_transcript.name,
            "publication_transcript_sha256": sha256_file(publication_transcript),
        },
        "pr_status_before": {
            "ok": True,
            "merge_ready": False,
            "pr_url": pr_url,
            "commit_sha": before_commit,
            "blockers": ["checks_failed"],
            "review_feedback": {"total": 0, "items": []},
            "failed_checks": [
                {
                    "name": "test",
                    "status": "COMPLETED",
                    "conclusion": "FAILURE",
                    "head_sha": before_commit,
                    "url": f"https://github.com/acme/chili/actions/runs/{pr_number}",
                    "completed_at": "2026-06-03T10:40:00Z",
                }
            ],
        },
        "repair_result": {
            "status": "success",
            "final_commit_sha": final_commit,
            "final_files_changed": ["tests/test_canonical_outcome_layer.py"],
            "handoff": {
                "publication_state": "repair_not_published",
                "post_repair_monitoring": {
                    "required_after_publication": True,
                    "pr_identifier": str(pr_number),
                    "expected_branch": "codex/stock-momentum-context-gate",
                },
            },
        },
        "publication_result": {
            "publication_state": "pr_published",
            "commit_sha": final_commit,
            "pr_output": pr_url,
            "post_repair_status": {
                "ok": True,
                "merge_ready": True,
                "pr_url": pr_url,
                "commit_sha": final_commit,
                "checks": {"total": 1, "passed": 1, "failed": 0, "pending": 0},
                "successful_checks": [
                    {
                        "name": "test",
                        "status": "COMPLETED",
                        "conclusion": "SUCCESS",
                        "head_sha": final_commit,
                        "url": f"https://github.com/acme/chili/actions/runs/{pr_number + 1}",
                        "completed_at": "2026-06-03T11:06:58Z",
                    }
                ],
            },
        },
    }
    return artifact, failure_transcript, publication_transcript


def _write_jsonl(path: Path, events: Sequence[Mapping[str, object]]) -> None:
    path.write_text(
        "\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n",
        encoding="utf-8",
    )


def write_artifact(path: Path, artifact: Mapping[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        key: value
        for key, value in artifact.items()
        if not str(key).startswith("_")
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _looks_like_hosted_pr_repair_artifact_file(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, Mapping)
        and payload.get("schema") == HOSTED_PR_REPAIR_ARTIFACT_SCHEMA_VERSION
        and payload.get("hosted") is True
    )


def discover_artifact_paths(paths: Sequence[Path], *, artifact_dir: Path | None = None) -> list[Path]:
    discovered = [path for path in paths if path.is_file()]
    if artifact_dir is not None:
        if not artifact_dir.is_dir():
            raise HostedPrRepairEvidenceError(f"artifact directory does not exist: {artifact_dir}")
        discovered.extend(
            sorted(
                path
                for path in artifact_dir.rglob("*.json")
                if path.is_file() and _looks_like_hosted_pr_repair_artifact_file(path)
            )
        )
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in discovered:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)
    return unique


def validate_hosted_pr_repair_artifact_inventory(
    paths: Sequence[Path],
    *,
    min_artifacts: int = 1,
    require_distinct_prs: bool = True,
) -> dict[str, object]:
    if len(paths) < min_artifacts:
        raise HostedPrRepairEvidenceError(
            f"at least {min_artifacts} hosted PR repair artifact(s) are required"
        )
    summaries: list[dict[str, object]] = []
    seen_prs: dict[str, Path] = {}
    seen_runs: dict[str, Path] = {}
    seen_artifact_ids: dict[str, Path] = {}
    for path in paths:
        artifact = load_artifact(path)
        summary = validate_hosted_pr_repair_artifact(artifact, base_dir=path.parent)
        pr_url = str(summary["pr_url"])
        source_run_id = str(summary["source_run_id"])
        artifact_id = str(summary["artifact_id"])
        if require_distinct_prs and pr_url in seen_prs:
            raise HostedPrRepairEvidenceError(f"duplicate pr_url in hosted repair inventory: {pr_url}")
        if source_run_id in seen_runs:
            raise HostedPrRepairEvidenceError(f"duplicate source_run_id in hosted repair inventory: {source_run_id}")
        if artifact_id in seen_artifact_ids:
            raise HostedPrRepairEvidenceError(f"duplicate artifact_id in hosted repair inventory: {artifact_id}")
        seen_prs[pr_url] = path
        seen_runs[source_run_id] = path
        seen_artifact_ids[artifact_id] = path
        summaries.append(summary)
    return {
        "schema": HOSTED_PR_REPAIR_BENCHMARK_SCHEMA_VERSION,
        "validated_hosted_pr_repair_artifact_inventory": True,
        "artifacts": len(summaries),
        "pr_urls": sorted(seen_prs),
        "source_agents": sorted({str(summary["source_agent"]) for summary in summaries}),
        "source_run_ids": sorted(seen_runs),
        "validated_hosted_pr_repair_artifacts": summaries,
    }


def _write_inventory_artifacts(root: Path, *, duplicate_pr: bool = False, duplicate_run: bool = False) -> list[Path]:
    first, _review, _publication = _valid_artifact_context(
        root / "first",
        pr_number=4242,
        final_commit="a" * 40,
        artifact_id="hosted-pr-repair-20260602T145000Z-first",
        source_run_id="codex-hosted-pr-repair-20260602T145000Z-first",
    )
    second, _review2, _publication2 = _valid_artifact_context(
        root / "second",
        pr_number=4242 if duplicate_pr else 4243,
        final_commit="b" * 40,
        artifact_id="hosted-pr-repair-20260602T145000Z-second",
        source_run_id=(
            "codex-hosted-pr-repair-20260602T145000Z-first"
            if duplicate_run
            else "codex-hosted-pr-repair-20260602T145000Z-second"
        ),
    )
    return [
        write_artifact(root / "first" / "artifact.json", first),
        write_artifact(root / "second" / "artifact.json", second),
    ]


def _mutate_artifact(
    check_id: str,
    artifact: dict[str, object],
    review_transcript: Path,
    publication_transcript: Path,
) -> None:
    if check_id == "self_test_artifact_rejected":
        artifact["source_run_id"] = "codex-self-test"
    elif check_id == "missing_review_thread_transcript_rejected":
        review_transcript.unlink()
    elif check_id == "sparse_review_transcript_rejected":
        review_transcript.write_text(
            json.dumps({"event": "review_thread_captured", "thread_id": "PRRT_kwDOHostedThread"}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        evidence = _as_mapping(artifact["evidence"], label="evidence")
        evidence["review_thread_transcript_sha256"] = sha256_file(review_transcript)
    elif check_id == "review_transcript_pr_mismatch_rejected":
        wrong_pr_url = "https://github.com/acme/chili/pull/9999"
        _write_jsonl(
            review_transcript,
            [
                {
                    "event": "review_thread_captured",
                    "pr_url": wrong_pr_url,
                    "thread_id": "PRRT_kwDOHostedThread",
                    "path": "app/main.py",
                    "line": 7,
                },
                {
                    "event": "review_thread_comment",
                    "thread_id": "PRRT_kwDOHostedThread",
                    "author": "reviewer",
                    "path": "app/main.py",
                    "line": 7,
                    "body": "The hosted PR still returns the stale value here.",
                },
                {
                    "event": "review_thread_ingested",
                    "source": "github_review_threads",
                    "total": 1,
                    "unresolved": 1,
                },
            ],
        )
        evidence = _as_mapping(artifact["evidence"], label="evidence")
        evidence["review_thread_transcript_sha256"] = sha256_file(review_transcript)
    elif check_id == "review_transcript_thread_detail_mismatch_rejected":
        pr_url = str(artifact.get("pr_url") or "")
        _write_jsonl(
            review_transcript,
            [
                {
                    "event": "review_thread_captured",
                    "pr_url": pr_url,
                    "thread_id": "PRRT_kwDOHostedThread",
                    "path": "app/main.py",
                    "line": 7,
                },
                {
                    "event": "review_thread_comment",
                    "thread_id": "PRRT_kwDOHostedThread",
                    "author": "reviewer",
                    "path": "app/main.py",
                    "line": 8,
                    "body": "The hosted PR still returns the stale value here.",
                },
                {
                    "event": "review_thread_ingested",
                    "source": "github_review_threads",
                    "total": 1,
                    "unresolved": 1,
                },
            ],
        )
        evidence = _as_mapping(artifact["evidence"], label="evidence")
        evidence["review_thread_transcript_sha256"] = sha256_file(review_transcript)
    elif check_id == "missing_line_thread_rejected":
        status = _as_mapping(artifact["pr_status_before"], label="artifact.pr_status_before")
        feedback = _as_mapping(status["review_feedback"], label="review_feedback")
        feedback["items"] = []
    elif check_id == "missing_remote_publication_rejected":
        publication = _as_mapping(artifact["publication_result"], label="publication_result")
        publication["publication_state"] = "export_ready"
    elif check_id == "post_repair_head_mismatch_rejected":
        publication = _as_mapping(artifact["publication_result"], label="publication_result")
        post = _as_mapping(publication["post_repair_status"], label="post_repair_status")
        post["commit_sha"] = "b" * 40
    elif check_id == "missing_post_repair_check_receipt_rejected":
        publication = _as_mapping(artifact["publication_result"], label="publication_result")
        post = _as_mapping(publication["post_repair_status"], label="post_repair_status")
        post["successful_checks"] = []
    elif check_id == "transcript_hash_mismatch_rejected":
        evidence = _as_mapping(artifact["evidence"], label="evidence")
        evidence["publication_transcript_sha256"] = "0" * 64
    elif check_id == "sparse_publication_transcript_rejected":
        publication_transcript.write_text(
            json.dumps({"event": "operator_approved_repaired_publication", "commit_sha": "a" * 40}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        evidence = _as_mapping(artifact["evidence"], label="evidence")
        evidence["publication_transcript_sha256"] = sha256_file(publication_transcript)
    elif check_id == "publication_transcript_pr_mismatch_rejected":
        wrong_pr_url = "https://github.com/acme/chili/pull/9999"
        repair = _as_mapping(artifact["repair_result"], label="artifact.repair_result")
        final_commit = _required_text(repair, "final_commit_sha", label="artifact.repair_result")
        _write_jsonl(
            publication_transcript,
            [
                {
                    "event": "operator_approved_repaired_publication",
                    "pr_url": wrong_pr_url,
                    "commit_sha": final_commit,
                    "post_repair_merge_ready": True,
                },
                {
                    "event": "pr_published",
                    "publication_state": "pr_published",
                    "pr_url": wrong_pr_url,
                    "commit_sha": final_commit,
                },
                {
                    "event": "post_repair_check_receipt",
                    "name": "test",
                    "conclusion": "success",
                    "head_sha": final_commit,
                    "url": "https://github.com/acme/chili/actions/runs/9999",
                    "completed_at": "2026-06-02T14:59:00Z",
                },
            ],
        )
        evidence = _as_mapping(artifact["evidence"], label="evidence")
        evidence["publication_transcript_sha256"] = sha256_file(publication_transcript)
    elif check_id == "publication_transcript_commit_mismatch_rejected":
        pr_url = str(artifact.get("pr_url") or "")
        repair = _as_mapping(artifact["repair_result"], label="artifact.repair_result")
        final_commit = _required_text(repair, "final_commit_sha", label="artifact.repair_result")
        wrong_commit = "b" * 40 if final_commit.lower() != "b" * 40 else "c" * 40
        _write_jsonl(
            publication_transcript,
            [
                {
                    "event": "operator_approved_repaired_publication",
                    "pr_url": pr_url,
                    "commit_sha": final_commit,
                    "post_repair_merge_ready": True,
                },
                {
                    "event": "pr_published",
                    "publication_state": "pr_published",
                    "pr_url": pr_url,
                    "commit_sha": wrong_commit,
                },
                {
                    "event": "post_repair_check_receipt",
                    "name": "test",
                    "conclusion": "success",
                    "head_sha": final_commit,
                    "url": "https://github.com/acme/chili/actions/runs/4242",
                    "completed_at": "2026-06-02T14:59:00Z",
                },
            ],
        )
        evidence = _as_mapping(artifact["evidence"], label="evidence")
        evidence["publication_transcript_sha256"] = sha256_file(publication_transcript)


def evaluate_check(check: HostedPrRepairCheck) -> HostedPrRepairResult:
    return evaluate_check_with_inventory(check)


def evaluate_check_with_inventory(
    check: HostedPrRepairCheck,
    *,
    valid_paths: Sequence[Path] | None = None,
    min_artifacts: int = 1,
    require_distinct_prs: bool = True,
) -> HostedPrRepairResult:
    with tempfile.TemporaryDirectory(prefix="chili_hosted_pr_repair_artifact_") as raw_root:
        root = Path(raw_root)
        try:
            if check.check_id == "valid_hosted_pr_repair_accepts" and valid_paths:
                summary = validate_hosted_pr_repair_artifact(
                    load_artifact(valid_paths[0]),
                    base_dir=valid_paths[0].parent,
                )
                evidence = (
                    f"validated_hosted_pr_repair_artifact={summary['validated_hosted_pr_repair_artifact']}; "
                    f"line_threads={summary['line_threads']}; "
                    f"post_repair_merge_ready={summary['post_repair_merge_ready']}; "
                    f"check_receipts={summary['post_repair_check_receipts']}"
                )
            elif check.check_id == "valid_artifact_inventory_accepts":
                paths = list(valid_paths or _write_inventory_artifacts(root / "inventory"))
                summary = validate_hosted_pr_repair_artifact_inventory(
                    paths,
                    min_artifacts=max(1, int(min_artifacts or 1)),
                    require_distinct_prs=require_distinct_prs,
                )
                evidence = (
                    "validated_hosted_pr_repair_artifact_inventory="
                    f"{summary['validated_hosted_pr_repair_artifact_inventory']}; "
                    f"artifacts={summary['artifacts']}; prs={len(summary['pr_urls'])}"
                )
            elif check.check_id == "empty_artifact_inventory_rejected":
                validate_hosted_pr_repair_artifact_inventory([], min_artifacts=1)
                evidence = "unexpectedly accepted empty inventory"
            elif check.check_id == "duplicate_pr_artifact_rejected":
                paths = _write_inventory_artifacts(root / "inventory", duplicate_pr=True)
                validate_hosted_pr_repair_artifact_inventory(paths, min_artifacts=2)
                evidence = "unexpectedly accepted duplicate PR inventory"
            elif check.check_id == "duplicate_source_run_rejected":
                paths = _write_inventory_artifacts(root / "inventory", duplicate_run=True)
                validate_hosted_pr_repair_artifact_inventory(paths, min_artifacts=2)
                evidence = "unexpectedly accepted duplicate source-run inventory"
            else:
                artifact, review_transcript, publication_transcript = _valid_artifact_context(root)
                _mutate_artifact(check.check_id, artifact, review_transcript, publication_transcript)
                summary = validate_hosted_pr_repair_artifact(artifact, base_dir=root)
                evidence = (
                    f"validated_hosted_pr_repair_artifact={summary['validated_hosted_pr_repair_artifact']}; "
                    f"line_threads={summary['line_threads']}; "
                    f"post_repair_merge_ready={summary['post_repair_merge_ready']}; "
                    f"check_receipts={summary['post_repair_check_receipts']}"
                )
            actual_status = "accepted"
        except HostedPrRepairEvidenceError as exc:
            evidence = str(exc)
            actual_status = "rejected"
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
    return [check_id for check_id in REQUIRED_CHECKS if check_id not in covered]


def benchmark_status(results: Sequence[HostedPrRepairResult]) -> str:
    if (
        len(results) >= len(REQUIRED_CHECKS)
        and average_score(results) >= TARGET_SCORE
        and all(result.passed for result in results)
        and not missing_checks(results)
    ):
        return "passed"
    return "failed"


def promotion_eligibility(
    results: Sequence[HostedPrRepairResult],
    *,
    evidence_mode: str,
) -> tuple[bool, str]:
    if benchmark_status(results) != "passed":
        return False, "Hosted PR repair checks are incomplete or failed."
    if evidence_mode != REAL_INVENTORY_EVIDENCE_MODE:
        return (
            False,
            "Self-test evidence proves the validator only; promotion requires real_inventory artifacts with transcript-bound PR evidence.",
        )
    return True, (
        "Real hosted PR repair inventory is validated with transcript-bound PR repair "
        "cause, publication, and current-head check evidence."
    )


def render_scorecard(
    results: Sequence[HostedPrRepairResult],
    *,
    evidence_mode: str = SELF_TEST_EVIDENCE_MODE,
    inventory_summary: Mapping[str, object] | None = None,
    generated_at: datetime | None = None,
) -> str:
    generated_at = generated_at or datetime.now(timezone.utc)
    artifacts: object = "fixture"
    prs: object = "fixture"
    source_agents: object = "fixture"
    source_runs: object = "fixture"
    if inventory_summary is not None:
        artifacts = inventory_summary.get("artifacts", 0)
        pr_urls = inventory_summary.get("pr_urls")
        prs = len(pr_urls) if isinstance(pr_urls, list) else 0
        agents = inventory_summary.get("source_agents")
        source_agents = ", ".join(str(agent) for agent in agents) if isinstance(agents, list) else ""
        runs = inventory_summary.get("source_run_ids")
        source_runs = len(runs) if isinstance(runs, list) else 0
    eligible, promotion_reason = promotion_eligibility(results, evidence_mode=evidence_mode)
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
        f"- PRs: {prs}",
        f"- Source agents: {source_agents}",
        f"- Source runs: {source_runs}",
        f"- Required checks: {', '.join(REQUIRED_CHECKS)}",
        f"- Missing checks: {', '.join(missing_checks(results)) or 'none'}",
        f"- Promotion eligible: {str(eligible).lower()}",
        f"- Promotion reason: {promotion_reason}",
        "- Required behavior: hosted PR repair evidence must include transcript-bound review-thread or hosted CI failure provenance, remote publication evidence, post-repair PR status, and current-head check receipts before it can count toward frontier PR repair parity.",
        "- Safety: deterministic schema/hash checks only; no model calls, git action, runtime restart, deployment, database migration, broker call, or live-trading action.",
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


def run_hosted_pr_repair_artifact_benchmark(
    *,
    output_path: Path = DEFAULT_OUTPUT,
    write: bool = True,
) -> tuple[list[HostedPrRepairResult], str, Path]:
    results = [evaluate_check(check) for check in default_checks()]
    markdown = render_scorecard(results, evidence_mode=SELF_TEST_EVIDENCE_MODE)
    if write:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(markdown, encoding="utf-8")
    return results, markdown, output_path


def run_hosted_pr_repair_artifact_validation(
    paths: Sequence[Path],
    *,
    output_path: Path = DEFAULT_OUTPUT,
    write: bool = True,
    min_artifacts: int = 1,
    require_distinct_prs: bool = True,
) -> tuple[list[HostedPrRepairResult], str, Path, dict[str, object]]:
    summary = validate_hosted_pr_repair_artifact_inventory(
        paths,
        min_artifacts=max(1, int(min_artifacts or 1)),
        require_distinct_prs=require_distinct_prs,
    )
    results = [
        evaluate_check_with_inventory(
            check,
            valid_paths=paths,
            min_artifacts=min_artifacts,
            require_distinct_prs=require_distinct_prs,
        )
        for check in default_checks()
    ]
    markdown = render_scorecard(
        results,
        evidence_mode=REAL_INVENTORY_EVIDENCE_MODE,
        inventory_summary=summary,
    )
    if write:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(markdown, encoding="utf-8")
    return results, markdown, output_path, summary


def load_artifact(path: Path) -> dict[str, object]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise HostedPrRepairEvidenceError(f"{path} must contain a JSON object")
    raw["_artifact_dir"] = str(path.parent)
    return raw


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate hosted PR repair artifact evidence.")
    parser.add_argument("--artifact", type=Path, action="append", default=[], help="Hosted PR repair artifact JSON to validate.")
    parser.add_argument("--artifact-dir", type=Path, help="Directory containing hosted PR repair artifact JSON files.")
    parser.add_argument("--min-artifacts", type=int, default=1)
    parser.add_argument("--allow-duplicate-prs", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)

    if args.artifact or args.artifact_dir:
        paths = discover_artifact_paths(args.artifact, artifact_dir=args.artifact_dir)
        results, markdown, output_path, payload = run_hosted_pr_repair_artifact_validation(
            paths,
            output_path=args.output,
            write=not args.no_write,
            min_artifacts=max(1, int(args.min_artifacts or 1)),
            require_distinct_prs=not args.allow_duplicate_prs,
        )
        payload["status"] = "passed"
        payload["evidence_mode"] = REAL_INVENTORY_EVIDENCE_MODE
        payload["checks"] = len(results)
        payload["average_score"] = average_score(results)
        payload["promotion_eligible"], payload["promotion_reason"] = promotion_eligibility(
            results,
            evidence_mode=REAL_INVENTORY_EVIDENCE_MODE,
        )
        payload["output"] = str(output_path)
        print(json.dumps(payload, indent=2, sort_keys=True) if args.json else payload)
        return 0

    results, markdown, output_path = run_hosted_pr_repair_artifact_benchmark(
        output_path=args.output,
        write=not args.no_write,
    )
    if args.json:
        print(
            json.dumps(
                {
                    "schema": HOSTED_PR_REPAIR_BENCHMARK_SCHEMA_VERSION,
                    "status": benchmark_status(results),
                    "evidence_mode": SELF_TEST_EVIDENCE_MODE,
                    "checks": len(results),
                    "average_score": average_score(results),
                    "promotion_eligible": promotion_eligibility(
                        results,
                        evidence_mode=SELF_TEST_EVIDENCE_MODE,
                    )[0],
                    "promotion_reason": promotion_eligibility(
                        results,
                        evidence_mode=SELF_TEST_EVIDENCE_MODE,
                    )[1],
                    "output": str(output_path),
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(markdown if args.no_write else f"Wrote {output_path}")
    return 0 if benchmark_status(results) == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
