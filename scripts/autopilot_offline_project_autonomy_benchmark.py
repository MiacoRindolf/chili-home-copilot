from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app import openai_client  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import Base  # noqa: E402
from app.models import (  # noqa: E402
    ProjectAutonomyArchitectReview,
    ProjectAutonomyArtifact,
    ProjectAutonomyLearningSample,
    ProjectAutonomyLease,
    ProjectAutonomyMessage,
    ProjectAutonomyRun,
    ProjectAutonomyStep,
    ProjectDomainRun,
    User,
)
from app.models.code_brain import CodeInsight, CodeRepo  # noqa: E402
from app.services.context_brain import llm_gateway  # noqa: E402
from app.services.project_autonomy import orchestrator  # noqa: E402
from scripts.autopilot_replay_benchmark_common import (  # noqa: E402
    ReplayCheck,
    average_score,
    benchmark_status,
    render_scorecard,
    write_scorecard,
)


DEFAULT_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "OFFLINE_PROJECT_AUTONOMY_BENCHMARK.md"
SCHEMA = "chili.offline-project-autonomy-benchmark.v1"
PREMIUM_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "CLAUDE_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "PAID_OPENAI_API_KEY",
    "FRONTIER_API_KEY",
    "CHILI_FRONTIER_API_KEY",
    "LLM_API_KEY",
    "PREMIUM_API_KEY",
)


@dataclasses.dataclass(frozen=True)
class OfflineScenarioResult:
    model: str
    duration_seconds: float
    plan_files: tuple[str, ...]
    changed_files: tuple[str, ...]
    premium_call_count: int
    test_output: str
    semantic_review_passed: bool


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            User.__table__,
            CodeRepo.__table__,
            CodeInsight.__table__,
            ProjectDomainRun.__table__,
            ProjectAutonomyRun.__table__,
            ProjectAutonomyMessage.__table__,
            ProjectAutonomyStep.__table__,
            ProjectAutonomyArtifact.__table__,
            ProjectAutonomyArchitectReview.__table__,
            ProjectAutonomyLease.__table__,
            ProjectAutonomyLearningSample.__table__,
        ],
    )
    return sessionmaker(bind=engine)()


def _policy_check() -> ReplayCheck:
    policy = orchestrator.local_autonomy_dependency_policy()
    expected = {
        "mode": "local_offline_capable",
        "internet_required": False,
        "premium_models_required": False,
        "model_runtime": "ollama",
        "external_frontier_models": "benchmark_or_explicit_opt_in_only",
        "premium_fallback_inside_orchestrator": False,
    }
    mismatches = {
        key: {"expected": value, "actual": policy.get(key)}
        for key, value in expected.items()
        if policy.get(key) != value
    }
    frontier_default = settings.__class__.model_fields["chili_code_frontier_enabled"].default
    if frontier_default is not False:
        mismatches["chili_code_frontier_enabled.default"] = {
            "expected": False,
            "actual": frontier_default,
        }
    if mismatches:
        return ReplayCheck("local_dependency_policy", json.dumps(mismatches, sort_keys=True), 0)
    return ReplayCheck(
        "local_dependency_policy",
        "premium_models_required=false; internet_required=false; frontier_default=false; external models benchmark/opt-in only",
    )


def _run_offline_scenario() -> OfflineScenarioResult:
    premium_call_count = 0
    local_model_events: list[dict[str, Any]] = []

    def forbidden_premium_call(*_args, **_kwargs):
        nonlocal premium_call_count
        premium_call_count += 1
        raise AssertionError("premium model route was called during offline Project Autopilot proof")

    saved_env = {key: os.environ.get(key) for key in PREMIUM_ENV_VARS}
    saved_openai_chat = openai_client.chat
    saved_gateway_chat = llm_gateway.gateway_chat
    saved_ollama_chat = orchestrator.ollama_client.chat
    saved_frontier_enabled = settings.chili_code_frontier_enabled
    for key in PREMIUM_ENV_VARS:
        os.environ.pop(key, None)
    openai_client.chat = forbidden_premium_call
    llm_gateway.gateway_chat = forbidden_premium_call

    def recording_local_chat(*args, **kwargs):
        result = saved_ollama_chat(*args, **kwargs)
        local_model_events.append(
            {
                "ok": bool(result.ok),
                "model": str(result.model or ""),
                "error": str(result.error or ""),
                "response_preview": str(result.text or "")[:3000],
            }
        )
        return result

    orchestrator.ollama_client.chat = recording_local_chat
    settings.chili_code_frontier_enabled = False
    db = _session()
    started = time.perf_counter()
    try:
        model_info = orchestrator.select_local_model()
        model = str(model_info.get("model") or "")
        if not model:
            raise AssertionError(model_info.get("recommendation") or "No local coder model is installed")
        with tempfile.TemporaryDirectory(prefix="chili_offline_autonomy_") as tmp:
            repo_path = Path(tmp)
            source_path = repo_path / "app" / "service.py"
            test_path = repo_path / "tests" / "test_service.py"
            source_path.parent.mkdir(parents=True)
            test_path.parent.mkdir(parents=True)
            source_path.write_text(
                "def normalize_name(name: str) -> str:\n"
                "    return name\n\n\n"
                "def greet(name: str) -> str:\n"
                "    return f\"Hello, {normalize_name(name)}\"\n",
                encoding="utf-8",
                newline="\n",
            )
            test_source = (
                "import pytest\n\n"
                "from app.service import greet, normalize_name\n\n\n"
                "def test_normalize_name_strips_surrounding_whitespace():\n"
                "    assert normalize_name(\"  Ada  \" ) == \"Ada\"\n\n\n"
                "def test_normalize_name_rejects_blank_values():\n"
                "    with pytest.raises(ValueError, match=\"name must not be blank\"):\n"
                "        normalize_name(\"   \" )\n\n\n"
                "def test_greet_uses_the_normalized_name():\n"
                "    assert greet(\"  Ada  \" ) == \"Hello, Ada\"\n"
            )
            test_path.write_text(test_source, encoding="utf-8", newline="\n")
            test_hash_before = orchestrator.hashlib.sha256(test_source.encode("utf-8")).hexdigest()
            init = orchestrator._git(repo_path, ["init"], timeout=60)
            if init.returncode != 0:
                raise AssertionError(init.stderr or init.stdout or "git init failed")

            repo = CodeRepo(path=str(repo_path), name="offline-autonomy", active=True)
            db.add(repo)
            db.commit()
            prompt = (
                "Fix app/service.py so normalize_name strips surrounding whitespace, raises "
                "ValueError('name must not be blank') for blank values, and greet uses the normalized "
                "name. Keep tests/test_service.py unchanged and make its behavior tests pass."
            )
            run = ProjectAutonomyRun(
                run_id="offline_project_autonomy",
                repo_id=repo.id,
                prompt=prompt,
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
                        "file": "app/service.py",
                        "symbol": "normalize_name",
                        "relevance": 1.0,
                        "source": "offline_behavior_fixture",
                    },
                    {
                        "file": "tests/test_service.py",
                        "symbol": "test_normalize_name_strips_surrounding_whitespace",
                        "relevance": 0.95,
                        "source": "offline_behavior_fixture",
                    },
                ],
            }
            plan = orchestrator.build_local_plan(
                db,
                run,
                repo,
                context=context,
                repo_path=repo_path,
            )
            plan_files = tuple(
                str(item.get("path") or "")
                for item in (plan.get("files") or [])
                if isinstance(item, dict) and item.get("path")
            )
            if "app/service.py" not in plan_files:
                raise AssertionError(f"local plan omitted app/service.py: {plan_files}")
            approved_files = [
                {
                    "path": "app/service.py",
                    "action": "modify",
                    "description": prompt,
                }
            ]
            diffs = orchestrator.generate_diffs_from_plan(
                db,
                run,
                repo_path,
                approved_files,
            )
            if not diffs:
                raise AssertionError("local editor returned no applicable diff")
            orchestrator._apply_diffs(repo_path, diffs)
            if orchestrator.hashlib.sha256(test_path.read_bytes()).hexdigest() != test_hash_before:
                raise AssertionError("behavior tests changed during the offline repair")
            completed = subprocess.run(
                [sys.executable, "-m", "pytest", "tests/test_service.py", "-q"],
                cwd=repo_path,
                text=True,
                capture_output=True,
                timeout=120,
                check=False,
            )
            test_output = " | ".join(
                line.strip()
                for line in (completed.stdout + "\n" + completed.stderr).splitlines()
                if line.strip()
            )
            if completed.returncode != 0:
                raise AssertionError(test_output[:1200] or f"pytest exited {completed.returncode}")
            diff_text = "\n".join(diffs)
            changed_files = tuple(sorted(orchestrator._diff_chunks_by_new_path(diff_text)))
            semantic = orchestrator.semantic_patch_review_gate(
                plan,
                changed_files,
                diff_text=diff_text,
                validation=(
                    {
                        "status": "passed",
                        "test_files": ["tests/test_service.py"],
                    },
                ),
            )
            if not semantic.get("passed"):
                raise AssertionError(str(semantic.get("reason") or "semantic review failed"))
            if premium_call_count:
                raise AssertionError(f"premium model calls observed: {premium_call_count}")
            return OfflineScenarioResult(
                model=model,
                duration_seconds=round(time.perf_counter() - started, 3),
                plan_files=plan_files,
                changed_files=changed_files,
                premium_call_count=premium_call_count,
                test_output=test_output[:700],
                semantic_review_passed=True,
            )
    except Exception as exc:
        raise AssertionError(
            f"{type(exc).__name__}: {exc}; local_model_events="
            + json.dumps(local_model_events, sort_keys=True)
        ) from exc
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


def _offline_scenario_check() -> ReplayCheck:
    try:
        result = _run_offline_scenario()
    except Exception as exc:
        return ReplayCheck("offline_local_plan_edit_test_review", f"{type(exc).__name__}: {exc}", 0)
    return ReplayCheck(
        "offline_local_plan_edit_test_review",
        (
            f"model={result.model}; duration={result.duration_seconds}s; "
            f"plan_files={','.join(result.plan_files)}; changed_files={','.join(result.changed_files)}; "
            f"premium_calls={result.premium_call_count}; semantic_review={result.semantic_review_passed}; "
            f"tests={result.test_output}"
        ),
    )


def run_benchmark() -> tuple[list[ReplayCheck], str]:
    results = [_policy_check(), _offline_scenario_check()]
    markdown = render_scorecard(
        title="CHILI Offline Project Autonomy Benchmark",
        schema=SCHEMA,
        results=results,
        required_behavior=(
            "Project Autopilot must plan, patch, preserve behavior tests, validate, and review with "
            "premium credentials absent and every premium model route made fatal."
        ),
        safety=(
            "isolated temporary repository and in-memory database only; local Ollama inference is "
            "allowed; no premium model, real source edit, git publication, deployment, broker, or live-trading action"
        ),
    )
    return results, markdown


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run CHILI's premium-disconnected autonomy proof.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)
    results, markdown = run_benchmark()
    if not args.no_write:
        write_scorecard(markdown, args.output)
    status = benchmark_status(results)
    if args.json:
        print(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "status": status,
                    "average_score": average_score(results),
                    "checks": len(results),
                    "output": str(args.output),
                    "written": not args.no_write,
                    "results": [dataclasses.asdict(result) for result in results],
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
