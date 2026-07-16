from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app import openai_client  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import ProjectAutonomyRun  # noqa: E402
from app.models.code_brain import CodeRepo  # noqa: E402
from app.services.context_brain import llm_gateway  # noqa: E402
from app.services.project_autonomy import orchestrator  # noqa: E402
from scripts import autopilot_frontier_source_runner as frontier_runner  # noqa: E402
from scripts import autopilot_offline_project_autonomy_benchmark as offline_benchmark  # noqa: E402


SCHEMA = "chili.meso-project-workflow-tournament.v1"
DEFAULT_OUTPUT = (
    REPO_ROOT
    / "project_ws"
    / "AgentOps"
    / "MESO_PROJECT_WORKFLOW_TOURNAMENT_BENCHMARK.md"
)
DEFAULT_ARTIFACT_ROOT = (
    REPO_ROOT
    / "project_ws"
    / "AgentOps"
    / "system_level_tournaments"
    / "meso"
)
SOURCE_KINDS = ("local_model", "codex", "claude")
MAX_ATTEMPTS = 3
MODEL_NAMES = {
    "local_model": "qwen2.5-coder:7b",
    "codex": "gpt-5.6-sol",
    "claude": "claude-fable-5",
}


@dataclasses.dataclass(frozen=True)
class WorkflowTask:
    task_id: str
    title: str
    goal: str
    source_files: Mapping[str, str]
    visible_tests: Mapping[str, str]
    hidden_tests: Mapping[str, str]
    required_files: tuple[str, ...]

    @property
    def allowed_files(self) -> tuple[str, ...]:
        return tuple(sorted(self.source_files))


@dataclasses.dataclass(frozen=True)
class ContestantResult:
    task_id: str
    source_kind: str
    model_name: str
    duration_seconds: float
    attempts: int
    quality_score: int
    behavior_passed: bool
    scope_valid: bool
    tests_unchanged: bool
    coordinated_files_changed: bool
    semantic_review_passed: bool
    premium_calls: int | None
    changed_files: tuple[str, ...]
    validation_output: str
    failure: str
    final_diff: str
    model_events: tuple[Mapping[str, Any], ...] = ()

    @property
    def eligible(self) -> bool:
        return (
            self.behavior_passed
            and self.scope_valid
            and self.tests_unchanged
            and self.coordinated_files_changed
            and self.semantic_review_passed
        )


FrontierCall = Callable[[str, str, int, float], tuple[str, float, str]]
Progress = Callable[[str], None]


def _clean(source: str) -> str:
    return textwrap.dedent(source).lstrip("\n")


def default_tasks() -> tuple[WorkflowTask, ...]:
    return (
        WorkflowTask(
            task_id="one-based-page-envelope",
            title="One-based page envelope contract",
            goal=(
                "Repair the paging contract across app/query.py, app/paging.py, and app/api.py. "
                "parse_page must map missing or blank input to 1 and reject non-integers or values "
                "below 1 with ValueError('page must be a positive integer'). page_slice must use "
                "one-based pages and reject page_size below 1 with ValueError('page_size must be "
                "positive'). build_page must return page, items, has_next, and next_page, where "
                "next_page is page + 1 only when more items remain. Keep every test file unchanged."
            ),
            source_files={
                "app/query.py": _clean(
                    """
                    def parse_page(raw: str | None) -> int:
                        if raw is None:
                            return 0
                        return int(raw)
                    """
                ),
                "app/paging.py": _clean(
                    """
                    from collections.abc import Sequence
                    from typing import TypeVar

                    T = TypeVar("T")


                    def page_slice(items: Sequence[T], page: int, page_size: int) -> list[T]:
                        start = page * page_size
                        return list(items[start : start + page_size])
                    """
                ),
                "app/api.py": _clean(
                    """
                    from collections.abc import Sequence
                    from typing import Any

                    from app.paging import page_slice
                    from app.query import parse_page


                    def build_page(items: Sequence[Any], raw_page: str | None, page_size: int) -> dict[str, Any]:
                        page = parse_page(raw_page)
                        return {"page": page, "items": page_slice(items, page, page_size)}
                    """
                ),
            },
            visible_tests={
                "tests/test_paging.py": _clean(
                    """
                    from app.api import build_page
                    from app.query import parse_page


                    def test_default_page_is_one():
                        assert parse_page(None) == 1
                        assert parse_page("  ") == 1


                    def test_second_page_has_navigation_metadata():
                        result = build_page([1, 2, 3, 4, 5], "2", 2)
                        assert result == {
                            "page": 2,
                            "items": [3, 4],
                            "has_next": True,
                            "next_page": 3,
                        }
                    """
                )
            },
            hidden_tests={
                "hidden_tests/test_paging_edges.py": _clean(
                    """
                    import pytest

                    from app.api import build_page
                    from app.paging import page_slice
                    from app.query import parse_page


                    @pytest.mark.parametrize("raw", ["0", "-2", "abc"])
                    def test_bad_pages_fail_closed(raw):
                        with pytest.raises(ValueError, match="page must be a positive integer"):
                            parse_page(raw)


                    def test_page_size_must_be_positive():
                        with pytest.raises(ValueError, match="page_size must be positive"):
                            page_slice([1], 1, 0)


                    def test_last_page_has_no_next_page():
                        assert build_page([1, 2, 3], "2", 2) == {
                            "page": 2,
                            "items": [3],
                            "has_next": False,
                            "next_page": None,
                        }
                    """
                )
            },
            required_files=("app/api.py", "app/paging.py", "app/query.py"),
        ),
        WorkflowTask(
            task_id="bounded-retry-contract",
            title="Bounded retry policy contract",
            goal=(
                "Repair the retry contract across app/config.py, app/backoff.py, and app/worker.py. "
                "RetryPolicy.from_mapping must use defaults max_attempts=3, base_delay_ms=100, "
                "max_delay_ms=1000; require max_attempts >= 1, positive delays, and base_delay_ms "
                "<= max_delay_ms, raising ValueError('invalid retry policy') otherwise. "
                "retry_delays must return exactly max_attempts - 1 exponential delays capped at "
                "max_delay_ms. run_with_retry must call the operation immediately, retry only "
                "TransientError, sleep only between failed attempts, return the first success, "
                "and re-raise the final TransientError. Keep every test file unchanged."
            ),
            source_files={
                "app/config.py": _clean(
                    """
                    from dataclasses import dataclass
                    from typing import Mapping


                    @dataclass(frozen=True)
                    class RetryPolicy:
                        max_attempts: int = 3
                        base_delay_ms: int = 100
                        max_delay_ms: int = 1000

                        @classmethod
                        def from_mapping(cls, values: Mapping[str, str]) -> "RetryPolicy":
                            return cls(
                                max_attempts=int(values.get("MAX_ATTEMPTS", "0")),
                                base_delay_ms=int(values.get("BASE_DELAY_MS", "0")),
                                max_delay_ms=int(values.get("MAX_DELAY_MS", "0")),
                            )
                    """
                ),
                "app/backoff.py": _clean(
                    """
                    from app.config import RetryPolicy


                    def retry_delays(policy: RetryPolicy) -> tuple[int, ...]:
                        return tuple(policy.base_delay_ms * (2 ** attempt) for attempt in range(policy.max_attempts))
                    """
                ),
                "app/worker.py": _clean(
                    """
                    from collections.abc import Callable
                    from typing import TypeVar

                    from app.backoff import retry_delays
                    from app.config import RetryPolicy

                    T = TypeVar("T")


                    class TransientError(RuntimeError):
                        pass


                    def run_with_retry(operation: Callable[[], T], policy: RetryPolicy, sleep: Callable[[float], None]) -> T:
                        for delay_ms in retry_delays(policy):
                            sleep(delay_ms / 1000)
                            try:
                                return operation()
                            except Exception:
                                continue
                        return operation()
                    """
                ),
            },
            visible_tests={
                "tests/test_retry.py": _clean(
                    """
                    from app.backoff import retry_delays
                    from app.config import RetryPolicy
                    from app.worker import TransientError, run_with_retry


                    def test_defaults_and_capped_delays():
                        policy = RetryPolicy.from_mapping({})
                        assert policy == RetryPolicy(3, 100, 1000)
                        assert retry_delays(policy) == (100, 200)


                    def test_retry_sleeps_only_after_failure():
                        calls = []
                        sleeps = []

                        def operation():
                            calls.append("call")
                            if len(calls) < 3:
                                raise TransientError("later")
                            return "ok"

                        result = run_with_retry(operation, RetryPolicy(3, 50, 100), sleeps.append)
                        assert result == "ok"
                        assert calls == ["call", "call", "call"]
                        assert sleeps == [0.05, 0.1]
                    """
                )
            },
            hidden_tests={
                "hidden_tests/test_retry_edges.py": _clean(
                    """
                    import pytest

                    from app.backoff import retry_delays
                    from app.config import RetryPolicy
                    from app.worker import TransientError, run_with_retry


                    @pytest.mark.parametrize(
                        "values",
                        [
                            {"MAX_ATTEMPTS": "0"},
                            {"BASE_DELAY_MS": "0"},
                            {"MAX_DELAY_MS": "0"},
                            {"BASE_DELAY_MS": "200", "MAX_DELAY_MS": "100"},
                        ],
                    )
                    def test_invalid_policy_is_rejected(values):
                        with pytest.raises(ValueError, match="invalid retry policy"):
                            RetryPolicy.from_mapping(values)


                    def test_backoff_is_capped_and_has_retry_count_only():
                        assert retry_delays(RetryPolicy(5, 80, 150)) == (80, 150, 150, 150)


                    def test_permanent_error_is_not_retried():
                        calls = []

                        def operation():
                            calls.append(1)
                            raise ValueError("permanent")

                        with pytest.raises(ValueError, match="permanent"):
                            run_with_retry(operation, RetryPolicy(), lambda _delay: None)
                        assert calls == [1]


                    def test_final_transient_error_is_reraised():
                        with pytest.raises(TransientError, match="still down"):
                            run_with_retry(
                                lambda: (_ for _ in ()).throw(TransientError("still down")),
                                RetryPolicy(2, 10, 10),
                                lambda _delay: None,
                            )
                    """
                )
            },
            required_files=("app/backoff.py", "app/config.py", "app/worker.py"),
        ),
        WorkflowTask(
            task_id="idempotent-ledger-event",
            title="Idempotent ledger event contract",
            goal=(
                "Repair the ledger event contract across app/model.py, app/store.py, and "
                "app/service.py. Event.from_payload must trim and require non-empty event_id and "
                "account_id, parse amount through Decimal(str(value)), and reject non-finite or "
                "non-positive amounts with ValueError('invalid event'). LedgerStore.add_once must "
                "deduplicate by (account_id, event_id), return whether it inserted, and balance "
                "must sum Decimal amounts for one account. record_event must return accepted, "
                "event_id, account_id, and the account balance formatted with two decimal places; "
                "a duplicate must not change the balance. Keep every test file unchanged."
            ),
            source_files={
                "app/model.py": _clean(
                    """
                    from dataclasses import dataclass
                    from decimal import Decimal
                    from typing import Any, Mapping


                    @dataclass(frozen=True)
                    class Event:
                        event_id: str
                        account_id: str
                        amount: Decimal

                        @classmethod
                        def from_payload(cls, payload: Mapping[str, Any]) -> "Event":
                            return cls(
                                event_id=str(payload.get("event_id", "")),
                                account_id=str(payload.get("account_id", "")),
                                amount=Decimal(payload.get("amount", 0)),
                            )
                    """
                ),
                "app/store.py": _clean(
                    """
                    from decimal import Decimal

                    from app.model import Event


                    class LedgerStore:
                        def __init__(self) -> None:
                            self.events: list[Event] = []

                        def add_once(self, event: Event) -> bool:
                            self.events.append(event)
                            return True

                        def balance(self, account_id: str) -> Decimal:
                            return sum((event.amount for event in self.events), Decimal("0"))
                    """
                ),
                "app/service.py": _clean(
                    """
                    from typing import Any, Mapping

                    from app.model import Event
                    from app.store import LedgerStore


                    def record_event(payload: Mapping[str, Any], store: LedgerStore) -> dict[str, Any]:
                        event = Event.from_payload(payload)
                        store.add_once(event)
                        return {"balance": str(store.balance(event.account_id))}
                    """
                ),
            },
            visible_tests={
                "tests/test_ledger.py": _clean(
                    """
                    from app.service import record_event
                    from app.store import LedgerStore


                    def test_duplicate_event_is_idempotent():
                        store = LedgerStore()
                        payload = {"event_id": " evt-1 ", "account_id": " acct-1 ", "amount": "2.50"}
                        assert record_event(payload, store) == {
                            "accepted": True,
                            "event_id": "evt-1",
                            "account_id": "acct-1",
                            "balance": "2.50",
                        }
                        assert record_event(payload, store) == {
                            "accepted": False,
                            "event_id": "evt-1",
                            "account_id": "acct-1",
                            "balance": "2.50",
                        }
                    """
                )
            },
            hidden_tests={
                "hidden_tests/test_ledger_edges.py": _clean(
                    """
                    import pytest

                    from app.model import Event
                    from app.service import record_event
                    from app.store import LedgerStore


                    @pytest.mark.parametrize(
                        "payload",
                        [
                            {"event_id": " ", "account_id": "a", "amount": "1"},
                            {"event_id": "e", "account_id": " ", "amount": "1"},
                            {"event_id": "e", "account_id": "a", "amount": "0"},
                            {"event_id": "e", "account_id": "a", "amount": "NaN"},
                        ],
                    )
                    def test_invalid_events_fail_closed(payload):
                        with pytest.raises(ValueError, match="invalid event"):
                            Event.from_payload(payload)


                    def test_idempotency_key_is_scoped_to_account():
                        store = LedgerStore()
                        first = record_event({"event_id": "same", "account_id": "a", "amount": "1.25"}, store)
                        second = record_event({"event_id": "same", "account_id": "b", "amount": "3"}, store)
                        assert first["accepted"] is True
                        assert second["accepted"] is True
                        assert first["balance"] == "1.25"
                        assert second["balance"] == "3.00"
                    """
                )
            },
            required_files=("app/model.py", "app/service.py", "app/store.py"),
        ),
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_id(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip(".-") or "run"


def _write_files(root: Path, files: Mapping[str, str]) -> None:
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")


def _init_task_repo(task: WorkflowTask, root: Path) -> None:
    _write_files(root, {**task.source_files, **task.visible_tests})
    for package in {Path(path).parent for path in task.source_files if "/" in path}:
        init_path = root / package / "__init__.py"
        if not init_path.exists():
            init_path.write_text("", encoding="utf-8")
    completed = orchestrator._git(root, ["init"], timeout=60)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout or "git init failed")
    orchestrator._git(root, ["config", "user.name", "CHILI Tournament"], timeout=30)
    orchestrator._git(root, ["config", "user.email", "tournament@localhost"], timeout=30)
    orchestrator._git(root, ["add", "."], timeout=30)
    committed = orchestrator._git(root, ["commit", "-m", "fixture"], timeout=60)
    if committed.returncode != 0:
        raise RuntimeError(committed.stderr or committed.stdout or "fixture commit failed")


def _hash_files(root: Path, paths: Sequence[str]) -> dict[str, str]:
    return {
        path: hashlib.sha256((root / path).read_bytes()).hexdigest()
        for path in paths
    }


def _source_snapshot(task: WorkflowTask, root: Path) -> str:
    sections: list[str] = []
    for relative in sorted((*task.source_files, *task.visible_tests)):
        sections.extend(
            [
                f"### {relative}",
                "```python",
                (root / relative).read_text(encoding="utf-8", errors="replace").rstrip(),
                "```",
                "",
            ]
        )
    return "\n".join(sections).rstrip()


def render_frontier_prompt(
    task: WorkflowTask,
    root: Path,
    *,
    validation_failure: str = "",
    previous_response: str = "",
) -> str:
    parts = [
        "You are competing in a measured multi-file repository repair tournament.",
        "Return one applicable unified diff only, preferably in a ```diff fence.",
        "Do not edit tests, add dependencies, use placeholders, or touch files outside the approved source files.",
        "Preserve public behavior not changed by the goal.",
        "",
        f"Goal: {task.goal}",
        f"Approved source files: {', '.join(task.allowed_files)}",
        "",
        "Repository snapshot:",
        _source_snapshot(task, root),
    ]
    if validation_failure:
        parts.extend(
            [
                "",
                "The previous attempt failed validation. Repair the current snapshot using this evidence:",
                validation_failure[-5000:],
                "",
                "Previous response:",
                previous_response[-5000:],
            ]
        )
    return "\n".join(parts).strip() + "\n"


def extract_unified_diff(response: str) -> str | None:
    for match in re.finditer(r"```(?:diff)?\s*\n(.*?)\n```", response or "", re.DOTALL):
        candidate = match.group(1).strip()
        if "--- " in candidate and "+++ " in candidate:
            return candidate + "\n"
    lines = (response or "").splitlines()
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if line.startswith("diff --git ") or line.startswith("--- a/")
        ),
        None,
    )
    if start is None:
        return None
    candidate = "\n".join(lines[start:]).strip()
    return candidate + "\n" if "--- " in candidate and "+++ " in candidate else None


def _apply_scoped_patch(root: Path, task: WorkflowTask, response: str) -> tuple[bool, str, str]:
    patch = extract_unified_diff(response)
    if not patch:
        return False, "response did not contain a unified diff", ""
    patch = orchestrator._recount_unified_diff_hunks(patch)
    changed = set(orchestrator._diff_chunks_by_new_path(patch))
    allowed = set(task.allowed_files)
    if not changed or not changed.issubset(allowed):
        return (
            False,
            f"patch scope mismatch: changed={sorted(changed)} allowed={sorted(allowed)}",
            patch,
        )
    check = orchestrator._git(root, ["apply", "--check"], input_text=patch, timeout=60)
    if check.returncode != 0:
        canonical = orchestrator._canonicalize_diff_against_contents(
            patch,
            {
                relative: (root / relative).read_text(encoding="utf-8", errors="replace")
                for relative in changed
            },
        )
        if canonical:
            canonical_check = orchestrator._git(
                root,
                ["apply", "--check"],
                input_text=canonical,
                timeout=60,
            )
            if canonical_check.returncode == 0:
                patch = canonical
                check = canonical_check
    if check.returncode != 0:
        error = (check.stderr or check.stdout or "git apply --check failed").strip()
        return False, error[:1800], patch
    applied = orchestrator._git(root, ["apply"], input_text=patch, timeout=60)
    if applied.returncode != 0:
        return False, (applied.stderr or applied.stdout or "git apply failed").strip()[:1800], patch
    return True, "applied", patch


def _run_validation(root: Path, task: WorkflowTask) -> tuple[bool, str]:
    _write_files(root, task.hidden_tests)
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "hidden_tests", "-q"],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    output = "\n".join(
        line.rstrip()
        for line in (completed.stdout + "\n" + completed.stderr).splitlines()
        if line.strip()
    )
    return completed.returncode == 0, output[-5000:]


def _final_diff(root: Path, task: WorkflowTask) -> str:
    completed = orchestrator._git(
        root,
        ["diff", "--", *task.allowed_files],
        timeout=60,
    )
    return completed.stdout or ""


def _changed_files(diff_text: str) -> tuple[str, ...]:
    return tuple(sorted(orchestrator._diff_chunks_by_new_path(diff_text)))


def _semantic_review(task: WorkflowTask, changed_files: Sequence[str], diff_text: str) -> bool:
    plan = {
        "analysis": "Coordinate the named source contracts without changing tests.",
        "files": [
            {"path": path, "action": "modify", "description": task.goal}
            for path in task.required_files
        ],
        "success_criteria": [task.goal],
        "validation_targets": ["visible and held-out pytest behavior"],
        "risks": ["cross-file contract drift"],
    }
    review = orchestrator.semantic_patch_review_gate(
        plan,
        changed_files,
        diff_text=diff_text,
        validation=({"status": "passed", "test_files": sorted(task.visible_tests)},),
    )
    return bool(review.get("passed"))


def quality_score(
    *,
    behavior_passed: bool,
    scope_valid: bool,
    tests_unchanged: bool,
    coordinated_files_changed: bool,
    semantic_review_passed: bool,
) -> int:
    return (
        (65 if behavior_passed else 0)
        + (10 if scope_valid else 0)
        + (10 if tests_unchanged else 0)
        + (10 if coordinated_files_changed else 0)
        + (5 if semantic_review_passed else 0)
    )


def _default_frontier_call(
    source_kind: str,
    prompt: str,
    timeout_seconds: int,
    max_budget_usd: float,
) -> tuple[str, float, str]:
    model_name = MODEL_NAMES[source_kind]
    if source_kind == "codex":
        command = frontier_runner._codex_command(model_name)
    elif source_kind == "claude":
        auth_mode = frontier_runner._resolve_source_auth_mode("claude", "auto")
        command = frontier_runner._claude_command(
            model_name,
            max_budget_usd,
            source_auth_mode=auth_mode,
        )
    else:
        raise ValueError(f"unsupported frontier source: {source_kind}")
    started = time.perf_counter()
    completed = frontier_runner._run_command(command, timeout_seconds, prompt)
    duration = max(0.0, time.perf_counter() - started)
    if completed.returncode != 0:
        error = (completed.stderr or completed.stdout or "frontier command failed").strip()
        raise RuntimeError(error[:3000])
    return completed.stdout or "", duration, " ".join(command)


def _build_result(
    *,
    task: WorkflowTask,
    source_kind: str,
    root: Path,
    duration_seconds: float,
    attempts: int,
    test_hashes_before: Mapping[str, str],
    validation_passed: bool,
    validation_output: str,
    failure: str,
    premium_calls: int | None,
    model_events: Sequence[Mapping[str, Any]] = (),
) -> ContestantResult:
    diff_text = _final_diff(root, task)
    changed_files = _changed_files(diff_text)
    scope_valid = bool(changed_files) and set(changed_files).issubset(task.allowed_files)
    tests_unchanged = test_hashes_before == _hash_files(root, tuple(task.visible_tests))
    coordinated = set(task.required_files).issubset(changed_files)
    semantic = (
        _semantic_review(task, changed_files, diff_text)
        if validation_passed and scope_valid
        else False
    )
    score = quality_score(
        behavior_passed=validation_passed,
        scope_valid=scope_valid,
        tests_unchanged=tests_unchanged,
        coordinated_files_changed=coordinated,
        semantic_review_passed=semantic,
    )
    return ContestantResult(
        task_id=task.task_id,
        source_kind=source_kind,
        model_name=MODEL_NAMES[source_kind],
        duration_seconds=round(duration_seconds, 3),
        attempts=attempts,
        quality_score=score,
        behavior_passed=validation_passed,
        scope_valid=scope_valid,
        tests_unchanged=tests_unchanged,
        coordinated_files_changed=coordinated,
        semantic_review_passed=semantic,
        premium_calls=premium_calls,
        changed_files=changed_files,
        validation_output=validation_output[-2400:],
        failure=failure[:1800],
        final_diff=diff_text,
        model_events=tuple(model_events),
    )


def run_frontier_contestant(
    task: WorkflowTask,
    source_kind: str,
    root: Path,
    *,
    call: FrontierCall = _default_frontier_call,
    timeout_seconds: int = 900,
    max_budget_usd: float = 2.0,
    artifact_dir: Path | None = None,
) -> ContestantResult:
    test_hashes = _hash_files(root, tuple(task.visible_tests))
    duration = 0.0
    previous_response = ""
    failure = ""
    validation_output = "validation not run"
    validation_passed = False
    attempts = 0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        attempts = attempt
        prompt = render_frontier_prompt(
            task,
            root,
            validation_failure=failure or (validation_output if attempt > 1 else ""),
            previous_response=previous_response,
        )
        if artifact_dir:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / f"prompt_attempt_{attempt}.txt").write_text(prompt, encoding="utf-8")
        try:
            response, elapsed, command = call(
                source_kind,
                prompt,
                timeout_seconds,
                max_budget_usd,
            )
            duration += elapsed
        except Exception as exc:
            failure = f"{type(exc).__name__}: {exc}"
            break
        previous_response = response
        if artifact_dir:
            (artifact_dir / f"response_attempt_{attempt}.txt").write_text(response, encoding="utf-8")
            (artifact_dir / f"command_attempt_{attempt}.txt").write_text(command + "\n", encoding="utf-8")
        applied, apply_evidence, patch = _apply_scoped_patch(root, task, response)
        if artifact_dir and patch:
            (artifact_dir / f"patch_attempt_{attempt}.diff").write_text(patch, encoding="utf-8")
        if not applied:
            failure = "patch rejected: " + apply_evidence
            continue
        validation_passed, validation_output = _run_validation(root, task)
        if validation_passed:
            failure = ""
            break
        failure = "validation failed: " + validation_output
    return _build_result(
        task=task,
        source_kind=source_kind,
        root=root,
        duration_seconds=duration,
        attempts=attempts,
        test_hashes_before=test_hashes,
        validation_passed=validation_passed,
        validation_output=validation_output,
        failure=failure,
        premium_calls=None,
    )


def run_chili_contestant(
    task: WorkflowTask,
    root: Path,
    *,
    artifact_dir: Path | None = None,
    progress: Progress | None = None,
) -> ContestantResult:
    test_hashes = _hash_files(root, tuple(task.visible_tests))
    premium_calls = 0
    model_events: list[dict[str, Any]] = []

    def forbidden_premium_call(*_args, **_kwargs):
        nonlocal premium_calls
        premium_calls += 1
        raise AssertionError("premium model route called inside CHILI meso contestant")

    saved_env = {key: os.environ.get(key) for key in offline_benchmark.PREMIUM_ENV_VARS}
    saved_openai_chat = openai_client.chat
    saved_gateway_chat = llm_gateway.gateway_chat
    saved_ollama_chat = orchestrator.ollama_client.chat
    saved_frontier_enabled = settings.chili_code_frontier_enabled
    for key in offline_benchmark.PREMIUM_ENV_VARS:
        os.environ.pop(key, None)
    openai_client.chat = forbidden_premium_call
    llm_gateway.gateway_chat = forbidden_premium_call

    def recording_local_chat(*args, **kwargs):
        result = saved_ollama_chat(*args, **kwargs)
        model_events.append(
            {
                "ok": bool(result.ok),
                "model": str(result.model or ""),
                "latency_ms": result.latency_ms,
                "error": str(result.error or ""),
                "response": str(result.text or ""),
            }
        )
        if artifact_dir:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / "model_events.json").write_text(
                json.dumps(model_events, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        return result

    orchestrator.ollama_client.chat = recording_local_chat
    settings.chili_code_frontier_enabled = False
    db = offline_benchmark._session()
    duration = 0.0
    attempts = 0
    validation_output = "validation not run"
    validation_passed = False
    failure = ""
    try:
        repo = CodeRepo(path=str(root), name=f"meso-{task.task_id}", active=True)
        db.add(repo)
        db.commit()
        run = ProjectAutonomyRun(
            run_id=f"meso-{task.task_id}-{int(time.time() * 1000)}",
            repo_id=repo.id,
            prompt=task.goal,
            status="running",
            current_stage="plan",
        )
        db.add(run)
        db.commit()
        context: dict[str, Any] = {
            "repos": [],
            "insights": [],
            "hotspots": [],
            "disable_adaptive_investigation": True,
            "relevant_files": [
                {
                    "file": path,
                    "symbol": Path(path).stem,
                    "relevance": 1.0 if path in task.source_files else 0.9,
                    "source": "meso_equal_context_fixture",
                }
                for path in sorted((*task.source_files, *task.visible_tests))
            ],
        }
        if progress:
            progress(f"{task.task_id}/local_model plan start")
        started = time.perf_counter()
        plan = orchestrator.build_local_plan(
            db,
            run,
            repo,
            context=context,
            repo_path=root,
        )
        duration += time.perf_counter() - started
        if progress:
            progress(
                f"{task.task_id}/local_model plan complete "
                f"elapsed={round(duration, 3)}s"
            )
        plan_files = {
            str(item.get("path") or "")
            for item in (plan.get("files") or [])
            if isinstance(item, Mapping)
        }
        missing_plan_files = set(task.required_files) - plan_files
        if missing_plan_files:
            failure = "local plan omitted required contract files: " + ", ".join(sorted(missing_plan_files))
        else:
            approved = [
                {"path": path, "action": "modify", "description": task.goal}
                for path in task.required_files
            ]
            for attempt in range(1, MAX_ATTEMPTS + 1):
                attempts = attempt
                if progress:
                    progress(f"{task.task_id}/local_model edit attempt={attempt} start")
                started = time.perf_counter()
                try:
                    diffs = orchestrator.generate_diffs_from_plan(
                        db,
                        run,
                        root,
                        approved,
                        validation_context=(failure if attempt > 1 else None),
                    )
                    orchestrator._apply_diffs(root, diffs)
                except Exception as exc:
                    duration += time.perf_counter() - started
                    failure = f"{type(exc).__name__}: {exc}"
                    if progress:
                        progress(
                            f"{task.task_id}/local_model edit attempt={attempt} rejected "
                            f"elapsed={round(duration, 3)}s reason={failure[:240]}"
                        )
                    continue
                duration += time.perf_counter() - started
                if progress:
                    progress(
                        f"{task.task_id}/local_model edit attempt={attempt} applied "
                        f"elapsed={round(duration, 3)}s"
                    )
                validation_passed, validation_output = _run_validation(root, task)
                if progress:
                    progress(
                        f"{task.task_id}/local_model validation attempt={attempt} "
                        f"passed={validation_passed}"
                    )
                if validation_passed:
                    failure = ""
                    break
                failure = "validation failed: " + validation_output
        if premium_calls:
            validation_passed = False
            failure = f"premium model calls observed: {premium_calls}"
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        db.close()
        openai_client.chat = saved_openai_chat
        llm_gateway.gateway_chat = saved_gateway_chat
        orchestrator.ollama_client.chat = saved_ollama_chat
        settings.chili_code_frontier_enabled = saved_frontier_enabled
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    result = _build_result(
        task=task,
        source_kind="local_model",
        root=root,
        duration_seconds=duration,
        attempts=attempts,
        test_hashes_before=test_hashes,
        validation_passed=validation_passed,
        validation_output=validation_output,
        failure=failure,
        premium_calls=premium_calls,
        model_events=model_events,
    )
    if artifact_dir:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "model_events.json").write_text(
            json.dumps(model_events, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        (artifact_dir / "final.diff").write_text(result.final_diff, encoding="utf-8")
    return result


def choose_winner(results: Sequence[ContestantResult]) -> ContestantResult | None:
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


def _collection_failure(result: ContestantResult) -> bool:
    return (
        result.source_kind in {"codex", "claude"}
        and not result.final_diff.strip()
        and result.failure.startswith("RuntimeError:")
    )


def _result_payload(result: ContestantResult) -> dict[str, Any]:
    payload = dataclasses.asdict(result)
    payload["eligible"] = result.eligible
    payload["model_events"] = [
        {key: value for key, value in event.items() if key != "response"}
        for event in result.model_events
    ]
    payload.pop("final_diff", None)
    return payload


def run_tournament(
    *,
    tasks: Sequence[WorkflowTask] | None = None,
    source_kinds: Sequence[str] = SOURCE_KINDS,
    frontier_call: FrontierCall = _default_frontier_call,
    timeout_seconds: int = 900,
    max_budget_usd: float = 2.0,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
    run_id: str | None = None,
    write: bool = True,
    progress: Progress | None = None,
) -> dict[str, Any]:
    selected_tasks = tuple(tasks or default_tasks())
    clean_sources = tuple(dict.fromkeys(str(value).strip() for value in source_kinds))
    invalid_sources = sorted(set(clean_sources) - set(SOURCE_KINDS))
    if invalid_sources:
        raise ValueError("unsupported source kinds: " + ", ".join(invalid_sources))
    clean_run_id = _safe_id(
        run_id or f"meso-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    if write:
        run_dir = artifact_root / clean_run_id
        if run_dir.exists():
            raise ValueError(f"artifact run already exists: {run_dir}")
        run_dir.mkdir(parents=True, exist_ok=False)
        cleanup = None
    else:
        cleanup = tempfile.TemporaryDirectory(prefix="chili_meso_tournament_")
        run_dir = Path(cleanup.name)
    all_results: list[ContestantResult] = []
    task_rows: list[dict[str, Any]] = []
    try:
        for task in selected_tasks:
            task_results: list[ContestantResult] = []
            for source_kind in clean_sources:
                if progress:
                    progress(f"{task.task_id}/{source_kind} start")
                with tempfile.TemporaryDirectory(prefix=f"meso_{task.task_id}_{source_kind}_") as tmp:
                    repo_path = Path(tmp)
                    _init_task_repo(task, repo_path)
                    artifact_dir = run_dir / task.task_id / source_kind if write else None
                    if source_kind == "local_model":
                        result = run_chili_contestant(
                            task,
                            repo_path,
                            artifact_dir=artifact_dir,
                            progress=progress,
                        )
                    else:
                        result = run_frontier_contestant(
                            task,
                            source_kind,
                            repo_path,
                            call=frontier_call,
                            timeout_seconds=timeout_seconds,
                            max_budget_usd=max_budget_usd,
                            artifact_dir=artifact_dir,
                        )
                    if artifact_dir:
                        (artifact_dir / "result.json").write_text(
                            json.dumps(_result_payload(result), indent=2, sort_keys=True),
                            encoding="utf-8",
                        )
                        (artifact_dir / "final.diff").write_text(result.final_diff, encoding="utf-8")
                    task_results.append(result)
                    all_results.append(result)
                if progress:
                    progress(
                        f"{task.task_id}/{source_kind} complete score={result.quality_score} "
                        f"passed={result.behavior_passed} seconds={result.duration_seconds}"
                    )
            winner = choose_winner(task_results)
            task_rows.append(
                {
                    "task_id": task.task_id,
                    "title": task.title,
                    "winner": winner.source_kind if winner else "none",
                    "winner_model": winner.model_name if winner else "none",
                    "winner_reason": (
                        "correctness/safety quality; zero-premium tie-break; then measured runtime"
                        if winner
                        else "no contestant passed all correctness and safety gates"
                    ),
                    "results": [_result_payload(result) for result in task_results],
                }
            )
        winner_counts = Counter(str(row["winner"]) for row in task_rows)
        for source in (*SOURCE_KINDS, "none"):
            winner_counts.setdefault(source, 0)
        required_result_count = len(selected_tasks) * len(clean_sources)
        exact_models = all(
            result.model_name == MODEL_NAMES[result.source_kind]
            for result in all_results
        )
        collection_failures = [
            f"{result.task_id}/{result.source_kind}: {result.failure}"
            for result in all_results
            if _collection_failure(result)
        ]
        status = (
            "passed"
            if len(selected_tasks) >= 3
            and set(clean_sources) == set(SOURCE_KINDS)
            and len(all_results) == required_result_count
            and exact_models
            and not collection_failures
            and winner_counts["none"] == 0
            else "failed"
        )
        summary = {
            "schema": SCHEMA,
            "generated_utc": _utc_now(),
            "status": status,
            "evidence_mode": "real_artifacts",
            "run_id": clean_run_id,
            "artifact_root": str(run_dir),
            "tasks": len(selected_tasks),
            "source_kinds": list(clean_sources),
            "model_names": {source: MODEL_NAMES[source] for source in clean_sources},
            "winner_counts": dict(winner_counts),
            "runtime_measurements": {
                "measured": len(all_results),
                "unmeasured": 0,
            },
            "collection_failures": collection_failures,
            "premium_independent_local_results": sum(
                1
                for result in all_results
                if result.source_kind == "local_model" and result.premium_calls == 0
            ),
            "task_results": task_rows,
            "safety": (
                "isolated temporary repositories only; premium frontier models are benchmark "
                "opponents; CHILI premium routes are fatal; no real source edit, git publication, "
                "deployment, database migration, broker, or live-trading action"
            ),
        }
        if write:
            (run_dir / "summary.json").write_text(
                json.dumps(summary, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        return summary
    finally:
        if cleanup is not None:
            cleanup.cleanup()


def _counts_text(counts: Mapping[str, Any]) -> str:
    return ", ".join(
        f"{source}={int(counts.get(source, 0) or 0)}"
        for source in ("local_model", "codex", "claude", "none")
    )


def render_report(summary: Mapping[str, Any]) -> str:
    runtime = summary.get("runtime_measurements") or {}
    lines = [
        "# CHILI Meso Project Workflow Tournament",
        "",
        f"- Schema: {SCHEMA}",
        f"- Generated UTC: {summary.get('generated_utc', '')}",
        f"- Status: {summary.get('status', 'failed')}",
        f"- Evidence mode: {summary.get('evidence_mode', 'missing')}",
        f"- Run id: {summary.get('run_id', 'missing')}",
        f"- Tasks: {summary.get('tasks', 0)}",
        f"- Source kinds: {', '.join(summary.get('source_kinds') or [])}",
        f"- Winner counts: {_counts_text(summary.get('winner_counts') or {})}",
        f"- Collection failures: {len(summary.get('collection_failures') or [])}",
        (
            "- Runtime measurements: "
            f"measured={runtime.get('measured', 0)}, unmeasured={runtime.get('unmeasured', 0)}"
        ),
        (
            "- Premium-independent local results: "
            f"{summary.get('premium_independent_local_results', 0)}/{summary.get('tasks', 0)}"
        ),
        "- Winner rule: correctness and safety quality first; when quality is equal, zero-premium operational independence; then measured runtime.",
        f"- Safety: {summary.get('safety', '')}",
        "",
        "| Task | Winner | Model | Local | Codex | Fable 5 | Evidence |",
        "| --- | --- | --- | ---: | ---: | ---: | --- |",
    ]
    for task_row in summary.get("task_results") or []:
        by_source = {
            str(result.get("source_kind")): result
            for result in (task_row.get("results") or [])
        }
        evidence_parts = []
        for source in SOURCE_KINDS:
            result = by_source.get(source) or {}
            evidence_parts.append(
                f"{source}:behavior={result.get('behavior_passed', False)},"
                f"scope={result.get('scope_valid', False)},"
                f"premium_calls={result.get('premium_calls', 'n/a')},"
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
                    "; ".join(evidence_parts),
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run equal-goal CHILI/Codex/Fable meso project workflow tournament."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--run-id")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--max-budget-usd", type=float, default=2.0)
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    summary = run_tournament(
        timeout_seconds=max(60, int(args.timeout_seconds)),
        max_budget_usd=max(0.1, float(args.max_budget_usd)),
        artifact_root=args.artifact_root,
        run_id=args.run_id,
        write=not args.no_write,
        progress=lambda message: print(
            f"[meso-tournament] {message}",
            file=sys.stderr,
            flush=True,
        ),
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
