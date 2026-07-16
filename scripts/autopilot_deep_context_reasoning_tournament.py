from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.models import ProjectAutonomyRun  # noqa: E402
from app.models.code_brain import CodeRepo  # noqa: E402
from app.services.project_autonomy import orchestrator  # noqa: E402
from scripts import autopilot_meso_project_workflow_tournament as meso  # noqa: E402
from scripts import autopilot_offline_project_autonomy_benchmark as offline_benchmark  # noqa: E402


SCHEMA = "chili.deep-context-reasoning-tournament.v1"
DEFAULT_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "DEEP_CONTEXT_REASONING_TOURNAMENT_BENCHMARK.md"
DEFAULT_ARTIFACT_ROOT = (
    REPO_ROOT / "project_ws" / "AgentOps" / "system_level_tournaments" / "deep_context"
)
SOURCE_KINDS = meso.SOURCE_KINDS
MODEL_NAMES = meso.MODEL_NAMES
MAX_ATTEMPTS = 2


@dataclasses.dataclass(frozen=True)
class ContextTask:
    task_id: str
    title: str
    goal: str
    source_files: Mapping[str, str]
    visible_tests: Mapping[str, str]
    hidden_tests: Mapping[str, str]
    contract_files: tuple[str, ...]

    @property
    def allowed_files(self) -> tuple[str, ...]:
        return tuple(sorted(self.source_files))

    @property
    def distractor_count(self) -> int:
        return len(self.source_files) - len(self.contract_files)


@dataclasses.dataclass(frozen=True)
class ContextContestantResult:
    task_id: str
    source_kind: str
    model_name: str
    duration_seconds: float
    attempts: int
    quality_score: int
    behavior_passed: bool
    scope_valid: bool
    context_scope_precise: bool
    tests_unchanged: bool
    semantic_review_passed: bool
    premium_calls: int | None
    context_files: int
    distractor_files: int
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
            and self.context_scope_precise
            and self.tests_unchanged
            and self.semantic_review_passed
        )


FrontierCall = meso.FrontierCall
Progress = Callable[[str], None]


def _clean(value: str) -> str:
    import textwrap

    return textwrap.dedent(value).lstrip("\n")


def _distractors(namespace: str) -> dict[str, str]:
    title = "".join(part.title() for part in namespace.split("_"))
    return {
        f"distractors/{namespace}/module_{index:02d}.py": _clean(
            f'''
            """Archived {namespace.replace('_', ' ')} compatibility component {index}."""
            from dataclasses import dataclass
            from typing import Any, Mapping


            @dataclass(frozen=True)
            class Archived{title}Record{index}:
                key: str
                metadata: Mapping[str, Any]


            def evaluate_{namespace}_{index}(value: Mapping[str, Any]) -> dict[str, Any]:
                return {{
                    "component": "{namespace}-{index}",
                    "active": bool(value.get("active", False)),
                    "metadata": dict(value),
                }}


            def format_{namespace}_{index}(record: Archived{title}Record{index}) -> str:
                return f"{{record.key}}:{index}"
            '''
        )
        for index in range(1, 21)
    }


def default_tasks() -> tuple[ContextTask, ...]:
    auth_contract = {
        "domain/claims.py": _clean(
            '''
            from dataclasses import dataclass
            from typing import Any, Mapping


            @dataclass(frozen=True)
            class Claims:
                subject_id: str
                tenant_id: str
                roles: tuple[str, ...]
                token: str = ""

                @classmethod
                def from_mapping(cls, value: Mapping[str, Any]) -> "Claims":
                    return cls(
                        subject_id=str(value.get("subject_id", "")),
                        tenant_id=str(value.get("tenant_id", "")),
                        roles=tuple(value.get("roles", ())),
                        token=str(value.get("token", "")),
                    )
            '''
        ),
        "domain/policy.py": _clean(
            '''
            def can_access(claims, resource):
                return True, "allowed"
            '''
        ),
        "api/handler.py": _clean(
            '''
            def handle_request(claims_payload, resource, audit):
                return {"status": 200}
            '''
        ),
        "audit/events.py": _clean(
            '''
            def record_decision(audit, claims, resource, allowed, reason):
                audit.append({"claims": vars(claims), "resource": dict(resource), "allowed": allowed})
            '''
        ),
    }
    auth_sources = {**auth_contract, **_distractors("authorization_policy")}
    auth = ContextTask(
        task_id="tenant-authorization-trace",
        title="Tenant authorization trace",
        goal=(
            "Deep-context repository contract repair. Trace the unique owners of Claims, "
            "can_access, handle_request, and record_decision across the repository; no target "
            "paths are provided and similarly themed files are distractors. Claims.from_mapping "
            "must trim required subject_id and tenant_id, normalize unique lowercase roles, retain "
            "token only internally, and raise ValueError('invalid claims') when invalid. can_access "
            "must deny cross-tenant access before any role, then allow same-tenant admin, then owner, "
            "else deny with reasons tenant_mismatch, tenant_admin, owner, or not_authorized. "
            "handle_request returns status, reason, subject_id, tenant_id and always calls "
            "record_decision. Audit records contain only subject_id, tenant_id, resource_id, allowed, "
            "and reason, never token or full payload. Change only the minimal repository contract owners."
        ),
        source_files=auth_sources,
        visible_tests={
            "tests/test_authorization_contract.py": _clean(
                '''
                from api.handler import handle_request


                def test_same_tenant_admin_is_allowed_and_audit_is_secret_free():
                    audit = []
                    result = handle_request(
                        {"subject_id": " user ", "tenant_id": " t1 ", "roles": ["ADMIN", "admin"], "token": "secret"},
                        {"resource_id": "r1", "tenant_id": "t1", "owner_id": "other"},
                        audit,
                    )
                    assert result == {"status": 200, "reason": "tenant_admin", "subject_id": "user", "tenant_id": "t1"}
                    assert audit == [{"subject_id": "user", "tenant_id": "t1", "resource_id": "r1", "allowed": True, "reason": "tenant_admin"}]
                    assert "secret" not in repr(audit)
                '''
            )
        },
        hidden_tests={
            "hidden_tests/test_authorization_edges.py": _clean(
                '''
                import pytest

                from api.handler import handle_request
                from domain.claims import Claims


                def test_cross_tenant_admin_is_denied_before_role():
                    audit = []
                    result = handle_request(
                        {"subject_id": "u", "tenant_id": "a", "roles": ["admin"]},
                        {"resource_id": "r", "tenant_id": "b", "owner_id": "u"},
                        audit,
                    )
                    assert result["status"] == 403
                    assert result["reason"] == "tenant_mismatch"


                def test_owner_and_non_owner_paths_are_distinct():
                    assert handle_request(
                        {"subject_id": "u", "tenant_id": "t", "roles": []},
                        {"resource_id": "r", "tenant_id": "t", "owner_id": "u"},
                        [],
                    )["reason"] == "owner"
                    assert handle_request(
                        {"subject_id": "u", "tenant_id": "t", "roles": []},
                        {"resource_id": "r", "tenant_id": "t", "owner_id": "x"},
                        [],
                    )["reason"] == "not_authorized"


                @pytest.mark.parametrize("payload", [{}, {"subject_id": " ", "tenant_id": "t"}])
                def test_invalid_claims_fail_closed(payload):
                    with pytest.raises(ValueError, match="invalid claims"):
                        Claims.from_mapping(payload)
                '''
            )
        },
        contract_files=tuple(sorted(auth_contract)),
    )

    cache_contract = {
        "domain/revision.py": _clean(
            '''
            from dataclasses import dataclass


            @dataclass(frozen=True)
            class RevisionToken:
                entity_id: str
                revision: int

                @classmethod
                def from_payload(cls, value):
                    return cls(str(value.get("entity_id", "")), int(value.get("revision", 0)))
            '''
        ),
        "cache/keys.py": _clean(
            '''
            def build_cache_key(token):
                return f"catalog:{token.entity_id}"
            '''
        ),
        "cache/store.py": _clean(
            '''
            class CacheStore:
                def __init__(self):
                    self.value = None

                def get(self, token):
                    return self.value

                def put(self, token, value):
                    self.value = value
                    return True
            '''
        ),
        "service/catalog.py": _clean(
            '''
            def load_catalog(token_payload, store, fetcher):
                return {"source": "origin", "value": fetcher("", 0), "revision": 0}
            '''
        ),
    }
    cache_sources = {**cache_contract, **_distractors("catalog_cache")}
    cache = ContextTask(
        task_id="revision-cache-trace",
        title="Revision cache trace",
        goal=(
            "Deep-context repository contract repair. Trace the unique owners of RevisionToken, "
            "build_cache_key, CacheStore, and load_catalog across the repository; no target paths "
            "are provided and similarly themed files are distractors. RevisionToken.from_payload "
            "must trim entity_id, require revision >= 1, and raise ValueError('invalid revision token'). "
            "build_cache_key includes entity and revision. CacheStore stores exact revision values, "
            "tracks latest revision per entity, rejects stale put operations without mutation, and "
            "invalidate_before removes older revisions and returns the removal count. load_catalog "
            "returns cache on exact hit, origin after accepted fetch, or stale_rejected with no stale "
            "value after a rejected write. Change only the minimal repository contract owners."
        ),
        source_files=cache_sources,
        visible_tests={
            "tests/test_revision_cache_contract.py": _clean(
                '''
                from cache.store import CacheStore
                from service.catalog import load_catalog


                def test_exact_revision_is_cached_without_refetch():
                    store = CacheStore()
                    calls = []

                    def fetch(entity_id, revision):
                        calls.append((entity_id, revision))
                        return {"name": "catalog"}

                    payload = {"entity_id": " item ", "revision": "2"}
                    assert load_catalog(payload, store, fetch)["source"] == "origin"
                    assert load_catalog(payload, store, fetch)["source"] == "cache"
                    assert calls == [("item", 2)]
                '''
            )
        },
        hidden_tests={
            "hidden_tests/test_revision_cache_edges.py": _clean(
                '''
                import pytest

                from cache.store import CacheStore
                from domain.revision import RevisionToken
                from service.catalog import load_catalog


                @pytest.mark.parametrize("payload", [{}, {"entity_id": "x", "revision": 0}])
                def test_invalid_token_fails_closed(payload):
                    with pytest.raises(ValueError, match="invalid revision token"):
                        RevisionToken.from_payload(payload)


                def test_stale_write_is_rejected_and_old_entries_can_be_invalidated():
                    store = CacheStore()
                    latest = RevisionToken("x", 5)
                    older = RevisionToken("x", 4)
                    assert store.put(latest, "new") is True
                    assert store.put(older, "old") is False
                    result = load_catalog({"entity_id": "x", "revision": 4}, store, lambda entity, revision: "old")
                    assert result == {"source": "stale_rejected", "value": None, "revision": 4}
                    assert store.put(RevisionToken("x", 6), "newer") is True
                    assert store.invalidate_before("x", 6) == 1
                    assert store.get(latest) is None
                '''
            )
        },
        contract_files=tuple(sorted(cache_contract)),
    )

    billing_contract = {
        "money/amount.py": _clean(
            '''
            from dataclasses import dataclass


            @dataclass(frozen=True)
            class Money:
                currency: str
                amount: float

                @classmethod
                def from_value(cls, currency, value):
                    return cls(currency, float(value))

                def formatted(self):
                    return str(self.amount)
            '''
        ),
        "billing/discounts.py": _clean(
            '''
            def apply_discount(subtotal, basis_points):
                return 0, subtotal
            '''
        ),
        "billing/tax.py": _clean(
            '''
            def apply_tax(net, basis_points):
                return 0
            '''
        ),
        "billing/invoice.py": _clean(
            '''
            def build_invoice(currency, subtotal_value, discount_basis_points, tax_basis_points):
                return {"currency": currency, "total": str(subtotal_value)}
            '''
        ),
    }
    billing_sources = {**billing_contract, **_distractors("billing_money")}
    billing = ContextTask(
        task_id="decimal-billing-trace",
        title="Decimal billing trace",
        goal=(
            "Deep-context repository contract repair. Trace the unique owners of Money, "
            "apply_discount, apply_tax, and build_invoice across the repository; no target paths "
            "are provided and similarly themed files are distractors. Money.from_value must use "
            "Decimal(str(value)), uppercase a three-letter currency, reject negative or non-finite "
            "values with ValueError('invalid money'), and quantize cents with ROUND_HALF_UP. "
            "apply_discount and apply_tax accept basis points 0..10000, reject others with "
            "ValueError('invalid basis points'), and return quantized Money. Tax is calculated after "
            "discount. build_invoice returns currency, subtotal, discount, net, tax, and total as "
            "two-decimal strings without float arithmetic. Change only the minimal repository contract owners."
        ),
        source_files=billing_sources,
        visible_tests={
            "tests/test_billing_contract.py": _clean(
                '''
                from billing.invoice import build_invoice
                from money.amount import Money


                def test_invoice_uses_decimal_rounding_and_tax_after_discount():
                    assert Money.from_value("usd", "1.005").formatted() == "1.01"
                    assert build_invoice("usd", "10.005", 1000, 825) == {
                        "currency": "USD",
                        "subtotal": "10.01",
                        "discount": "1.00",
                        "net": "9.01",
                        "tax": "0.74",
                        "total": "9.75",
                    }
                '''
            )
        },
        hidden_tests={
            "hidden_tests/test_billing_edges.py": _clean(
                '''
                from decimal import Decimal

                import pytest

                from billing.discounts import apply_discount
                from billing.tax import apply_tax
                from money.amount import Money


                @pytest.mark.parametrize("currency,value", [("US", "1"), ("USD", "NaN"), ("USD", "-1")])
                def test_invalid_money_fails_closed(currency, value):
                    with pytest.raises(ValueError, match="invalid money"):
                        Money.from_value(currency, value)


                def test_money_stays_decimal_and_basis_points_are_bounded():
                    money = Money.from_value("eur", "3.33")
                    assert isinstance(money.amount, Decimal)
                    with pytest.raises(ValueError, match="invalid basis points"):
                        apply_discount(money, 10001)
                    with pytest.raises(ValueError, match="invalid basis points"):
                        apply_tax(money, -1)
                '''
            )
        },
        contract_files=tuple(sorted(billing_contract)),
    )
    return auth, cache, billing


def _write_files(root: Path, files: Mapping[str, str]) -> None:
    meso._write_files(root, files)


def _init_task_repo(task: ContextTask, root: Path) -> None:
    _write_files(root, {**task.source_files, **task.visible_tests})
    packages = {
        parent
        for path in (*task.source_files, *task.visible_tests)
        for parent in [Path(path).parent]
        if str(parent) not in {"", "."}
    }
    for package in packages:
        current = root / package
        current.mkdir(parents=True, exist_ok=True)
        while current != root:
            init_path = current / "__init__.py"
            if not init_path.exists():
                init_path.write_text("", encoding="utf-8")
            current = current.parent
    completed = orchestrator._git(root, ["init"], timeout=60)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout or "git init failed")
    orchestrator._git(root, ["config", "user.name", "CHILI Context Tournament"], timeout=30)
    orchestrator._git(root, ["config", "user.email", "context@localhost"], timeout=30)
    orchestrator._git(root, ["add", "."], timeout=30)
    committed = orchestrator._git(root, ["commit", "-m", "fixture"], timeout=60)
    if committed.returncode != 0:
        raise RuntimeError(committed.stderr or committed.stdout or "fixture commit failed")


def _snapshot(task: ContextTask, root: Path) -> str:
    sections = []
    for path in sorted((*task.source_files, *task.visible_tests)):
        sections.extend(
            [
                f"### {path}",
                "```python",
                (root / path).read_text(encoding="utf-8", errors="replace").rstrip(),
                "```",
                "",
            ]
        )
    return "\n".join(sections).rstrip()


def render_frontier_prompt(
    task: ContextTask,
    root: Path,
    *,
    validation_failure: str = "",
    previous_response: str = "",
) -> str:
    parts = [
        "You are competing in a measured deep-context repository reasoning tournament.",
        "Return one applicable unified diff only, preferably in a ```diff fence.",
        "Infer the minimal contract-owning files from symbols and repository relationships.",
        "Do not edit tests, add dependencies, touch distractors, use placeholders, or modify unrelated source files.",
        "Held-out tests are not shown.",
        "",
        f"Goal: {task.goal}",
        f"Repository source files in context: {len(task.source_files)}",
        "",
        "Repository snapshot:",
        _snapshot(task, root),
    ]
    if validation_failure:
        parts.extend(
            [
                "",
                "The previous patch failed validation. Repair the current snapshot using this evidence:",
                validation_failure[-6000:],
                "",
                "Previous response:",
                previous_response[-5000:],
            ]
        )
    return "\n".join(parts).strip() + "\n"


def _run_validation(root: Path, task: ContextTask) -> tuple[bool, str]:
    _write_files(root, task.hidden_tests)
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "hidden_tests", "-q"],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    output = "\n".join(
        line.rstrip()
        for line in (completed.stdout + "\n" + completed.stderr).splitlines()
        if line.strip()
    )
    return completed.returncode == 0, output[-6000:]


def _semantic_review(task: ContextTask, changed: Sequence[str], diff: str) -> bool:
    plan = {
        "analysis": "Resolve the named symbols from distractor-heavy repository context.",
        "files": [{"path": path, "action": "modify", "description": task.goal} for path in task.contract_files],
        "success_criteria": [task.goal, "No distractor or test file changes."],
        "validation_targets": sorted(task.visible_tests),
        "risks": ["wrong symbol owner", "cross-module contract drift"],
    }
    review = orchestrator.semantic_patch_review_gate(
        plan,
        list(changed),
        diff_text=diff,
        validation=({"status": "passed", "test_files": sorted(task.visible_tests)},),
    )
    return bool(review.get("passed"))


def quality_score(
    *,
    behavior_passed: bool,
    scope_valid: bool,
    context_scope_precise: bool,
    tests_unchanged: bool,
    semantic_review_passed: bool,
) -> int:
    return (
        (70 if behavior_passed else 0)
        + (10 if scope_valid else 0)
        + (10 if context_scope_precise else 0)
        + (5 if tests_unchanged else 0)
        + (5 if semantic_review_passed else 0)
    )


def _build_result(
    task: ContextTask,
    source_kind: str,
    root: Path,
    *,
    duration: float,
    attempts: int,
    test_hashes: Mapping[str, str],
    validation_passed: bool,
    validation_output: str,
    failure: str,
    premium_calls: int | None,
    model_events: Sequence[Mapping[str, Any]] = (),
) -> ContextContestantResult:
    diff = meso._final_diff(root, task)
    changed = meso._changed_files(diff)
    scope_valid = bool(changed) and set(changed).issubset(task.allowed_files)
    precise = set(changed) == set(task.contract_files)
    tests_unchanged = test_hashes == meso._hash_files(root, tuple(test_hashes))
    semantic = _semantic_review(task, changed, diff) if validation_passed and scope_valid else False
    score = quality_score(
        behavior_passed=validation_passed,
        scope_valid=scope_valid,
        context_scope_precise=precise,
        tests_unchanged=tests_unchanged,
        semantic_review_passed=semantic,
    )
    return ContextContestantResult(
        task_id=task.task_id,
        source_kind=source_kind,
        model_name=MODEL_NAMES[source_kind],
        duration_seconds=round(duration, 3),
        attempts=attempts,
        quality_score=score,
        behavior_passed=validation_passed,
        scope_valid=scope_valid,
        context_scope_precise=precise,
        tests_unchanged=tests_unchanged,
        semantic_review_passed=semantic,
        premium_calls=premium_calls,
        context_files=len(task.source_files),
        distractor_files=task.distractor_count,
        changed_files=changed,
        validation_output=validation_output[-3000:],
        failure=failure[:2400],
        final_diff=diff,
        model_events=tuple(dict(item) for item in model_events),
    )


def run_frontier_contestant(
    task: ContextTask,
    source_kind: str,
    root: Path,
    *,
    call: FrontierCall = meso._default_frontier_call,
    timeout_seconds: int = 900,
    max_budget_usd: float = 10.0,
    artifact_dir: Path | None = None,
) -> ContextContestantResult:
    test_hashes = meso._hash_files(root, tuple(task.visible_tests))
    duration = 0.0
    attempts = 0
    failure = ""
    output = "validation not run"
    passed = False
    previous = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        attempts = attempt
        prompt = render_frontier_prompt(
            task,
            root,
            validation_failure=failure,
            previous_response=previous,
        )
        if artifact_dir:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / f"prompt_attempt_{attempt}.txt").write_text(prompt, encoding="utf-8")
        try:
            response, elapsed, command = call(source_kind, prompt, timeout_seconds, max_budget_usd)
            duration += elapsed
        except Exception as exc:
            failure = f"RuntimeError: {exc}"
            break
        previous = response
        if artifact_dir:
            (artifact_dir / f"response_attempt_{attempt}.txt").write_text(response, encoding="utf-8")
            (artifact_dir / f"command_attempt_{attempt}.txt").write_text(command + "\n", encoding="utf-8")
        applied, evidence, patch = meso._apply_scoped_patch(root, task, response)
        if artifact_dir and patch:
            (artifact_dir / f"patch_attempt_{attempt}.diff").write_text(patch, encoding="utf-8")
        if not applied:
            failure = "patch rejected: " + evidence
            continue
        passed, output = _run_validation(root, task)
        if passed:
            failure = ""
            break
        failure = "validation failed: " + output
    return _build_result(
        task,
        source_kind,
        root,
        duration=duration,
        attempts=attempts,
        test_hashes=test_hashes,
        validation_passed=passed,
        validation_output=output,
        failure=failure,
        premium_calls=None,
    )


def run_chili_contestant(
    task: ContextTask,
    root: Path,
    *,
    artifact_dir: Path | None = None,
) -> ContextContestantResult:
    test_hashes = meso._hash_files(root, tuple(task.visible_tests))
    premium_calls = 0
    model_events: list[dict[str, Any]] = []

    def forbidden(*_args, **_kwargs):
        nonlocal premium_calls
        premium_calls += 1
        raise AssertionError("premium model route called inside CHILI deep-context contestant")

    saved_env = {key: os.environ.get(key) for key in offline_benchmark.PREMIUM_ENV_VARS}
    saved_openai = meso.openai_client.chat
    saved_gateway = meso.llm_gateway.gateway_chat
    saved_ollama = orchestrator.ollama_client.chat
    saved_frontier = meso.settings.chili_code_frontier_enabled
    for key in offline_benchmark.PREMIUM_ENV_VARS:
        os.environ.pop(key, None)
    meso.openai_client.chat = forbidden
    meso.llm_gateway.gateway_chat = forbidden

    def recording_local_chat(*args, **kwargs):
        result = saved_ollama(*args, **kwargs)
        model_events.append(
            {
                "ok": bool(result.ok),
                "model": str(result.model or ""),
                "latency_ms": result.latency_ms,
                "error": str(result.error or ""),
                "response": str(result.text or ""),
            }
        )
        return result

    orchestrator.ollama_client.chat = recording_local_chat
    meso.settings.chili_code_frontier_enabled = False
    db = offline_benchmark._session()
    duration = 0.0
    attempts = 0
    passed = False
    output = "validation not run"
    failure = ""
    try:
        repo = CodeRepo(path=str(root), name=f"context-{task.task_id}", active=True)
        db.add(repo)
        db.commit()
        run = ProjectAutonomyRun(
            run_id=f"context-{task.task_id}-{int(time.time() * 1000)}",
            repo_id=repo.id,
            prompt=task.goal,
            status="running",
            current_stage="plan",
        )
        db.add(run)
        db.commit()
        context = {
            "repos": [],
            "insights": [],
            "hotspots": [],
            "disable_adaptive_investigation": True,
            "relevant_files": [
                {
                    "file": path,
                    "symbol": Path(path).stem,
                    "relevance": 0.5,
                    "source": "deep_context_equal_snapshot",
                }
                for path in sorted((*task.source_files, *task.visible_tests))
            ],
        }
        started = time.perf_counter()
        plan = orchestrator.build_local_plan(db, run, repo, context=context, repo_path=root)
        duration += time.perf_counter() - started
        planned = {
            str(item.get("path") or "")
            for item in (plan.get("files") or [])
            if isinstance(item, Mapping)
        }
        if planned != set(task.contract_files):
            failure = f"context scope mismatch: planned={sorted(planned)} expected={sorted(task.contract_files)}"
        else:
            approved = [
                {"path": path, "action": "modify", "description": task.goal}
                for path in sorted(planned)
            ]
            for attempt in range(1, MAX_ATTEMPTS + 1):
                attempts = attempt
                started = time.perf_counter()
                try:
                    diffs = orchestrator.generate_diffs_from_plan(
                        db,
                        run,
                        root,
                        approved,
                        validation_context=failure if attempt > 1 else None,
                    )
                    orchestrator._apply_diffs(root, diffs)
                except Exception as exc:
                    duration += time.perf_counter() - started
                    failure = f"{type(exc).__name__}: {exc}"
                    continue
                duration += time.perf_counter() - started
                passed, output = _run_validation(root, task)
                if passed:
                    failure = ""
                    break
                failure = "validation failed: " + output
        if premium_calls:
            passed = False
            failure = f"premium model calls observed: {premium_calls}"
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        db.close()
        meso.openai_client.chat = saved_openai
        meso.llm_gateway.gateway_chat = saved_gateway
        orchestrator.ollama_client.chat = saved_ollama
        meso.settings.chili_code_frontier_enabled = saved_frontier
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    result = _build_result(
        task,
        "local_model",
        root,
        duration=duration,
        attempts=attempts,
        test_hashes=test_hashes,
        validation_passed=passed,
        validation_output=output,
        failure=failure,
        premium_calls=premium_calls,
        model_events=model_events,
    )
    if artifact_dir:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "model_events.json").write_text(json.dumps(model_events, indent=2), encoding="utf-8")
    return result


def choose_winner(results: Sequence[ContextContestantResult]) -> ContextContestantResult | None:
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


def _result_payload(result: ContextContestantResult) -> dict[str, Any]:
    payload = dataclasses.asdict(result)
    payload["eligible"] = result.eligible
    payload["model_events"] = [
        {key: value for key, value in event.items() if key != "response"}
        for event in result.model_events
    ]
    payload.pop("final_diff", None)
    return payload


def _collection_failure(result: ContextContestantResult) -> bool:
    return result.source_kind in {"codex", "claude"} and not result.final_diff.strip() and result.failure.startswith("RuntimeError:")


def run_tournament(
    *,
    tasks: Sequence[ContextTask] | None = None,
    source_kinds: Sequence[str] = SOURCE_KINDS,
    frontier_call: FrontierCall = meso._default_frontier_call,
    timeout_seconds: int = 900,
    max_budget_usd: float = 10.0,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
    run_id: str | None = None,
    write: bool = True,
    progress: Progress | None = None,
) -> dict[str, Any]:
    selected = tuple(tasks or default_tasks())
    sources = tuple(dict.fromkeys(str(value).strip() for value in source_kinds))
    invalid = sorted(set(sources) - set(SOURCE_KINDS))
    if invalid:
        raise ValueError("unsupported source kinds: " + ", ".join(invalid))
    clean_run_id = meso._safe_id(run_id or f"deep-context-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    if write:
        run_dir = artifact_root / clean_run_id
        if run_dir.exists():
            raise ValueError(f"artifact run already exists: {run_dir}")
        run_dir.mkdir(parents=True, exist_ok=False)
        cleanup = None
    else:
        cleanup = tempfile.TemporaryDirectory(prefix="chili_context_tournament_")
        run_dir = Path(cleanup.name)
    all_results: list[ContextContestantResult] = []
    task_rows = []
    try:
        for task in selected:
            results = []
            for source_kind in sources:
                if progress:
                    progress(f"{task.task_id}/{source_kind} start")
                with tempfile.TemporaryDirectory(prefix=f"context_{task.task_id}_{source_kind}_") as tmp:
                    root = Path(tmp)
                    _init_task_repo(task, root)
                    artifact_dir = run_dir / task.task_id / source_kind if write else None
                    if source_kind == "local_model":
                        result = run_chili_contestant(task, root, artifact_dir=artifact_dir)
                    else:
                        result = run_frontier_contestant(
                            task,
                            source_kind,
                            root,
                            call=frontier_call,
                            timeout_seconds=timeout_seconds,
                            max_budget_usd=max_budget_usd,
                            artifact_dir=artifact_dir,
                        )
                    if artifact_dir:
                        artifact_dir.mkdir(parents=True, exist_ok=True)
                        (artifact_dir / "result.json").write_text(json.dumps(_result_payload(result), indent=2, sort_keys=True), encoding="utf-8")
                        (artifact_dir / "final.diff").write_text(result.final_diff, encoding="utf-8")
                    results.append(result)
                    all_results.append(result)
                if progress:
                    progress(f"{task.task_id}/{source_kind} complete score={result.quality_score} passed={result.behavior_passed} seconds={result.duration_seconds}")
            winner = choose_winner(results)
            task_rows.append(
                {
                    "task_id": task.task_id,
                    "title": task.title,
                    "context_files": len(task.source_files),
                    "distractor_files": task.distractor_count,
                    "winner": winner.source_kind if winner else "none",
                    "winner_model": winner.model_name if winner else "none",
                    "winner_reason": "behavior and minimal-scope quality; zero-premium tie-break; then measured runtime" if winner else "no eligible contestant",
                    "results": [_result_payload(result) for result in results],
                }
            )
        winners = Counter(str(row["winner"]) for row in task_rows)
        for source in (*SOURCE_KINDS, "none"):
            winners.setdefault(source, 0)
        failures = [
            f"{result.task_id}/{result.source_kind}: {result.failure}"
            for result in all_results
            if _collection_failure(result)
        ]
        status = (
            "passed"
            if len(selected) >= 3
            and set(sources) == set(SOURCE_KINDS)
            and len(all_results) == len(selected) * len(sources)
            and not failures
            and winners["none"] == 0
            and all(result.model_name == MODEL_NAMES[result.source_kind] for result in all_results)
            else "failed"
        )
        summary = {
            "schema": SCHEMA,
            "generated_utc": meso._utc_now(),
            "status": status,
            "evidence_mode": "real_artifacts",
            "run_id": clean_run_id,
            "artifact_root": str(run_dir),
            "tasks": len(selected),
            "context_files_per_task": 24,
            "distractor_files_per_task": 20,
            "source_kinds": list(sources),
            "model_names": {source: MODEL_NAMES[source] for source in sources},
            "winner_counts": dict(winners),
            "runtime_measurements": {"measured": len(all_results), "unmeasured": 0},
            "collection_failures": failures,
            "premium_independent_local_results": sum(
                1 for result in all_results if result.source_kind == "local_model" and result.premium_calls == 0
            ),
            "task_results": task_rows,
            "safety": (
                "isolated temporary repositories only; exact Fable 5 and Codex 5.6 Sol are "
                "benchmark opponents; CHILI premium routes are fatal; no real source publication, "
                "deployment, runtime, broker, or live-trading action"
            ),
        }
        if write:
            (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
        return summary
    finally:
        if cleanup is not None:
            cleanup.cleanup()


def _counts_text(counts: Mapping[str, Any]) -> str:
    return ", ".join(f"{source}={int(counts.get(source, 0) or 0)}" for source in ("local_model", "codex", "claude", "none"))


def render_report(summary: Mapping[str, Any]) -> str:
    runtime = summary.get("runtime_measurements") or {}
    lines = [
        "# CHILI Deep-Context Reasoning Tournament",
        "",
        f"- Schema: {SCHEMA}",
        f"- Generated UTC: {summary.get('generated_utc', '')}",
        f"- Status: {summary.get('status', 'failed')}",
        f"- Evidence mode: {summary.get('evidence_mode', 'missing')}",
        f"- Run id: {summary.get('run_id', 'missing')}",
        f"- Tasks: {summary.get('tasks', 0)}",
        f"- Context files per task: {summary.get('context_files_per_task', 0)}",
        f"- Distractor files per task: {summary.get('distractor_files_per_task', 0)}",
        f"- Source kinds: {', '.join(summary.get('source_kinds') or [])}",
        f"- Winner counts: {_counts_text(summary.get('winner_counts') or {})}",
        f"- Collection failures: {len(summary.get('collection_failures') or [])}",
        f"- Runtime measurements: measured={runtime.get('measured', 0)}, unmeasured={runtime.get('unmeasured', 0)}",
        f"- Premium-independent local results: {summary.get('premium_independent_local_results', 0)}/{summary.get('tasks', 0)}",
        "- Winner rule: behavior and minimal-scope quality first; on an exact quality tie, zero-premium operational independence; then measured runtime.",
        f"- Safety: {summary.get('safety', '')}",
        "",
        "| Task | Winner | Model | Local | Codex | Fable 5 | Evidence |",
        "| --- | --- | --- | ---: | ---: | ---: | --- |",
    ]
    for task_row in summary.get("task_results") or []:
        by_source = {str(item.get("source_kind")): item for item in task_row.get("results") or []}
        evidence = []
        for source in SOURCE_KINDS:
            result = by_source.get(source) or {}
            evidence.append(
                f"{source}:behavior={result.get('behavior_passed', False)},"
                f"scope={result.get('context_scope_precise', False)},"
                f"premium_calls={result.get('premium_calls', 'n/a')},seconds={result.get('duration_seconds', 0)}"
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
                    "; ".join(evidence),
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run equal-goal CHILI/Codex/Fable deep-context tournament.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--run-id")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--max-budget-usd", type=float, default=10.0)
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    summary = run_tournament(
        timeout_seconds=max(60, args.timeout_seconds),
        max_budget_usd=max(0.1, args.max_budget_usd),
        artifact_root=args.artifact_root,
        run_id=args.run_id,
        write=not args.no_write,
        progress=lambda message: print(f"[deep-context-tournament] {message}", file=sys.stderr, flush=True),
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
