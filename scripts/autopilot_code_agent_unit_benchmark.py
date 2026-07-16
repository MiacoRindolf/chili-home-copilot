from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.code_brain import agent as code_agent  # noqa: E402
from app.services.project_autonomy import orchestrator  # noqa: E402


CODE_AGENT_UNIT_BENCHMARK_SCHEMA_VERSION = "chili.code-agent-unit-benchmark.v1"
VALID_SUITES = (
    "plan-safety",
    "request-preflight-safety",
    "diff-safety",
    "related-context",
)


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    suite: str
    status: str
    evidence: str

    @property
    def passed(self) -> bool:
        return self.status == "passed"


def _pass(check_id: str, suite: str, evidence: str) -> CheckResult:
    return CheckResult(check_id, suite, "passed", evidence)


def _fail(check_id: str, suite: str, evidence: str) -> CheckResult:
    return CheckResult(check_id, suite, "failed", evidence)


def _run_check(check_id: str, suite: str, fn: Callable[[], str]) -> CheckResult:
    try:
        evidence = fn()
    except AssertionError as exc:
        return _fail(check_id, suite, str(exc) or "assertion failed")
    except Exception as exc:
        return _fail(check_id, suite, f"{type(exc).__name__}: {exc}")
    return _pass(check_id, suite, evidence)


def _temp_repo() -> Path:
    root = Path(tempfile.mkdtemp(prefix="chili_code_agent_bench_"))
    app_dir = root / "app" / "services"
    app_dir.mkdir(parents=True)
    (app_dir / "safe_target.py").write_text(
        "def render_status(value):\n    return str(value)\n",
        encoding="utf-8",
    )
    return root


def _architect_review_for(
    *,
    prompt: str,
    path: str = "app/services/safe_target.py",
    description: str = "Update the status rendering behavior in this small support file.",
) -> dict[str, object]:
    repo = _temp_repo()
    plan = {
        "analysis": "Small targeted plan.",
        "files": [
            {
                "path": path,
                "action": "modify",
                "description": description,
            }
        ],
        "notes": "",
    }
    files = [
        {
            "path": path,
            "action": "modify",
            "description": description,
        }
    ]
    context = {"relevant_files": [{"file": path}], "insights": [], "hotspots": [], "repos": []}
    return orchestrator._review_architect_plan(
        plan=plan,
        files=files,
        context=context,
        repo_path=repo,
        prompt=prompt,
        attempt_index=1,
    )


def plan_safety_checks() -> list[CheckResult]:
    suite = "plan-safety"

    def prompt_contract() -> str:
        prompt = code_agent._build_plan_prompt(
            {
                "repos": [
                    {
                        "id": 1,
                        "name": "demo",
                        "path": "D:/demo",
                        "runtime_path": "D:/demo",
                        "file_count": 3,
                        "total_lines": 42,
                        "languages": {"Python": 42},
                        "frameworks": ["pytest"],
                    }
                ],
                "insights": [],
                "hotspots": [],
                "relevant_files": [{"file": "app/services/safe_target.py", "repo": "demo"}],
            }
        )
        assert "Return a JSON object" in prompt, "plan prompt must demand structured JSON"
        assert "Do NOT generate any code or diffs yet" in prompt, "plan prompt must separate planning from edits"
        assert "max 8" in prompt, "plan prompt must preserve scope bound"
        return "plan prompt requires JSON, no diffs, and bounded file count"

    def fenced_json_parse() -> str:
        parsed = code_agent._parse_plan_json(
            'Here is the plan:\n```json\n{"analysis":"ok","files":[{"path":"app/a.py","action":"modify","description":"tight change"}],"notes":""}\n```'
        )
        assert parsed is not None, "fenced JSON plan was not parsed"
        assert parsed["files"][0]["path"] == "app/a.py", "parsed file path mismatch"
        return "fenced JSON plan parsed with files"

    def plan_files_are_sanitized() -> str:
        files = orchestrator._plan_files(
            {
                "files": [
                    {"path": "../escape.py", "action": "modify", "description": "bad"},
                    {"path": "app/services/safe_target.py", "action": "modify", "description": "good"},
                    {"path": "app/services/safe_target.py", "action": "modify", "description": "duplicate"},
                ]
            }
        )
        assert [item["path"] for item in files] == ["app/services/safe_target.py"], "plan file sanitizer failed"
        return "unsafe paths rejected and duplicates collapsed"

    return [
        _run_check("plan_prompt_contract", suite, prompt_contract),
        _run_check("fenced_json_parse", suite, fenced_json_parse),
        _run_check("plan_files_are_sanitized", suite, plan_files_are_sanitized),
    ]


def request_preflight_checks() -> list[CheckResult]:
    suite = "request-preflight-safety"

    def destructive_prompt_blocked() -> str:
        review = _architect_review_for(prompt="Drop table users and delete database records.")
        blockers = review.get("critique", {}).get("blockers", [])
        assert "unsafe_or_destructive_action" in blockers, f"missing destructive blocker: {blockers}"
        assert review.get("status") == orchestrator.ARCHITECT_REVIEW_STATUS_FAILED, "destructive plan was not failed"
        return "architect review blocks destructive requests before implementation"

    def missing_file_blocked() -> str:
        review = _architect_review_for(
            prompt="Update the safe target.",
            path="app/services/not_present.py",
            description="Modify a missing file.",
        )
        blockers = review.get("critique", {}).get("blockers", [])
        assert "file_missing" in blockers, f"missing file blocker absent: {blockers}"
        return "architect review blocks plans that target missing files"

    return [
        _run_check("destructive_prompt_blocked", suite, destructive_prompt_blocked),
        _run_check("missing_file_blocked", suite, missing_file_blocked),
    ]


def diff_safety_checks() -> list[CheckResult]:
    suite = "diff-safety"

    def valid_diff_passes() -> str:
        content = "def render_status(value):\n    return str(value)\n"
        diff = (
            "--- a/app/services/safe_target.py\n"
            "+++ b/app/services/safe_target.py\n"
            "@@\n"
            "-    return str(value)\n"
            "+    return str(value).strip()\n"
        )
        result = code_agent._validate_diff(diff, "app/services/safe_target.py", content)
        assert result["valid"] is True, f"valid diff rejected: {result}"
        return "diff validator accepts removals present in the real file"

    def hallucinated_removal_rejected() -> str:
        content = "def render_status(value):\n    return str(value)\n"
        diff = (
            "--- a/app/services/safe_target.py\n"
            "+++ b/app/services/safe_target.py\n"
            "@@\n"
            "-def missing_one(): pass\n"
            "-def missing_two(): pass\n"
            "-def missing_three(): pass\n"
            "+def render_status(value):\n"
            "+    return str(value).strip()\n"
        )
        result = code_agent._validate_diff(diff, "app/services/safe_target.py", content)
        assert result["valid"] is False, f"hallucinated removals accepted: {result}"
        assert result["warnings"], "hallucinated removals should produce warnings"
        return "diff validator rejects hallucinated removed lines"

    def extracted_diff_preserves_unified_patch() -> str:
        diff = "```diff\n--- a/app/a.py\n+++ b/app/a.py\n@@\n-old\n+new\n```"
        extracted = orchestrator._extract_diff(diff)
        assert extracted and extracted.startswith("--- a/app/a.py"), "unified diff was not extracted"
        return "autonomy diff extractor preserves fenced unified patch"

    return [
        _run_check("valid_diff_passes", suite, valid_diff_passes),
        _run_check("hallucinated_removal_rejected", suite, hallucinated_removal_rejected),
        _run_check("extracted_diff_preserves_unified_patch", suite, extracted_diff_preserves_unified_patch),
    ]


def related_context_checks() -> list[CheckResult]:
    suite = "related-context"

    def related_files_in_prompt() -> str:
        prompt = code_agent._build_plan_prompt(
            {
                "repos": [],
                "insights": [],
                "hotspots": [],
                "relevant_files": [
                    {"file": "app/services/orders.py", "repo": "demo", "symbol": "create_order"},
                    {"file": "tests/test_orders.py", "repo": "demo"},
                ],
            }
        )
        assert "app/services/orders.py (contains: create_order)" in prompt, "symbol-bearing related file missing"
        assert "tests/test_orders.py" in prompt, "related test file missing"
        return "plan prompt carries related source and test context"

    def selected_file_rationale_uses_path_terms() -> str:
        rationale = orchestrator._selected_file_rationale(
            "Improve orders response handling",
            "app/services/orders.py",
            "",
        )
        assert "path shares concrete terms" in rationale.lower(), f"unexpected rationale: {rationale}"
        return "architect rationale ties selected files to request terms"

    return [
        _run_check("related_files_in_prompt", suite, related_files_in_prompt),
        _run_check("selected_file_rationale_uses_path_terms", suite, selected_file_rationale_uses_path_terms),
    ]


def run_suite(suite: str) -> list[CheckResult]:
    if suite == "plan-safety":
        return plan_safety_checks()
    if suite == "request-preflight-safety":
        return request_preflight_checks()
    if suite == "diff-safety":
        return diff_safety_checks()
    if suite == "related-context":
        return related_context_checks()
    raise ValueError(f"unknown suite: {suite}")


def render_payload(suite: str, results: Sequence[CheckResult]) -> dict[str, object]:
    return {
        "schema": CODE_AGENT_UNIT_BENCHMARK_SCHEMA_VERSION,
        "suite": suite,
        "status": "passed" if all(result.passed for result in results) else "failed",
        "checks": len(results),
        "passed": sum(1 for result in results if result.passed),
        "results": [
            {
                "check_id": result.check_id,
                "suite": result.suite,
                "status": result.status,
                "evidence": result.evidence,
            }
            for result in results
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run read-only CHILI code-agent unit benchmarks.")
    parser.add_argument("--suite", choices=VALID_SUITES, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    results = run_suite(args.suite)
    payload = render_payload(args.suite, results)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"CHILI code-agent benchmark suite {args.suite}: {payload['status']}")
        for result in results:
            print(f"- {result.check_id}: {result.status} - {result.evidence}")
    return 0 if payload["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
