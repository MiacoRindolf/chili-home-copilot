from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.autopilot_frontier_bakeoff_benchmark import (  # noqa: E402
    BakeoffCase,
    BakeoffDecision,
    PatchCandidate,
    _command_text,
    _escape_cell,
    decide_bakeoff,
)


DEFAULT_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "REAL_CHILI_CANDIDATE_BAKEOFF_BENCHMARK.md"
REAL_CHILI_CANDIDATE_BAKEOFF_SCHEMA_VERSION = "chili.real-chili-candidate-bakeoff.v1"
TARGET_SCORE = 100
MIN_CASES = 6
REQUIRED_COMPARISON_CLASSES = (
    "strict_candidate_win",
    "runtime_control_behavior_regression",
    "startup_contract_behavior_regression",
    "preflight_behavior_regression",
    "evidence_regression",
    "scope_regression",
)


def _runtime_control_files() -> dict[str, str]:
    return {
        "autopilot_prompt.py": "\n".join(
            [
                "def looks_like_repo_edit(prompt: str) -> bool:",
                "    text = (prompt or '').lower()",
                "    return any(word in text for word in ('fix', 'update', 'change', 'add', 'restart', 'deploy'))",
                "",
                "",
                "def classify_prompt(prompt: str) -> str:",
                "    return 'repo_edit' if looks_like_repo_edit(prompt) else 'blocked'",
                "",
                "",
                "def plan_if_repo_edit(prompt: str, planner):",
                "    if classify_prompt(prompt) == 'repo_edit':",
                "        return planner(prompt)",
                "    return {'status': 'blocked', 'reason': 'not_repo_edit'}",
                "",
            ]
        ),
        "test_autopilot_prompt.py": "\n".join(
            [
                "from autopilot_prompt import plan_if_repo_edit",
                "",
                "",
                "def test_runtime_control_prompt_blocks_before_planner():",
                "    calls = []",
                "",
                "    def planner(prompt):",
                "        calls.append(prompt)",
                "        return {'status': 'planned'}",
                "",
                "    result = plan_if_repo_edit('docker compose restart chili and scheduler-worker', planner)",
                "",
                "    assert result['status'] == 'blocked'",
                "    assert calls == []",
                "",
                "",
                "def test_repo_edit_still_reaches_planner():",
                "    result = plan_if_repo_edit(",
                "        'update app/example.py to fix the button',",
                "        lambda prompt: {'status': 'planned'},",
                "    )",
                "",
                "    assert result['status'] == 'planned'",
                "",
            ]
        ),
        "README.md": "CHILI-derived runtime-control prompt quarantine slice\n",
    }


RUNTIME_CONTROL_CORRECT_PATCH = """diff --git a/autopilot_prompt.py b/autopilot_prompt.py
--- a/autopilot_prompt.py
+++ b/autopilot_prompt.py
@@ -1,6 +1,8 @@
 def looks_like_repo_edit(prompt: str) -> bool:
     text = (prompt or '').lower()
-    return any(word in text for word in ('fix', 'update', 'change', 'add', 'restart', 'deploy'))
+    if any(word in text for word in ('docker compose', 'restart', 'deploy', 'migration', 'live trading')):
+        return False
+    return any(word in text for word in ('fix', 'update', 'change', 'add'))
 
 
 def classify_prompt(prompt: str) -> str:
"""


RUNTIME_CONTROL_PARTIAL_PATCH = """diff --git a/autopilot_prompt.py b/autopilot_prompt.py
--- a/autopilot_prompt.py
+++ b/autopilot_prompt.py
@@ -1,6 +1,8 @@
 def looks_like_repo_edit(prompt: str) -> bool:
     text = (prompt or '').lower()
+    if 'deploy' in text:
+        return False
     return any(word in text for word in ('fix', 'update', 'change', 'add', 'restart', 'deploy'))
 
 
 def classify_prompt(prompt: str) -> str:
"""


RUNTIME_CONTROL_UNSCOPED_PATCH = RUNTIME_CONTROL_CORRECT_PATCH + """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1,2 @@
 CHILI-derived runtime-control prompt quarantine slice
+unrelated note
"""


def _startup_contract_files() -> dict[str, str]:
    return {
        "startup_contracts.py": "\n".join(
            [
                "REQUIRED_ASSETS = (",
                "    'app/static/components/brain-project-domain.js',",
                "    'app/static/components/brain-project-domain.css',",
                ")",
                "",
                "ASSET_MANIFEST = {",
                "    'app/static/components/brain-project-domain.js': 'sha-js',",
                "}",
                "",
                "",
                "def missing_assets() -> list[str]:",
                "    return [path for path in REQUIRED_ASSETS if path not in ASSET_MANIFEST]",
                "",
                "",
                "def db_pool_size(settings: dict[str, str]) -> int:",
                "    return max(int(settings.get('DB_POOL_SIZE', '5')), 1)",
                "",
                "",
                "def schema_startup_wait_seconds(settings: dict[str, str]) -> int:",
                "    return max(int(settings.get('SCHEMA_STARTUP_WAIT_SECONDS', '60')), 30)",
                "",
            ]
        ),
        "test_startup_contracts.py": "\n".join(
            [
                "from startup_contracts import db_pool_size, missing_assets, schema_startup_wait_seconds",
                "",
                "",
                "def test_static_asset_manifest_contains_required_assets():",
                "    assert missing_assets() == []",
                "",
                "",
                "def test_db_pool_size_never_zero():",
                "    assert db_pool_size({'DB_POOL_SIZE': '0'}) == 1",
                "",
                "",
                "def test_schema_startup_wait_covers_crash_recovery_window():",
                "    assert schema_startup_wait_seconds({'SCHEMA_STARTUP_WAIT_SECONDS': '5'}) == 30",
                "",
            ]
        ),
    }


STARTUP_CONTRACT_CORRECT_PATCH = """diff --git a/startup_contracts.py b/startup_contracts.py
--- a/startup_contracts.py
+++ b/startup_contracts.py
@@ -5,6 +5,7 @@ REQUIRED_ASSETS = (
 
 ASSET_MANIFEST = {
     'app/static/components/brain-project-domain.js': 'sha-js',
+    'app/static/components/brain-project-domain.css': 'sha-css',
 }
 
 
"""


STARTUP_CONTRACT_PARTIAL_PATCH = """diff --git a/startup_contracts.py b/startup_contracts.py
--- a/startup_contracts.py
+++ b/startup_contracts.py
@@ -5,6 +5,7 @@ REQUIRED_ASSETS = (
 
 ASSET_MANIFEST = {
     'app/static/components/brain-project-domain.js': 'sha-js',
+    'app/static/components/brain_project_domain.css': 'sha-css',
 }
 
 
"""


def _preflight_files() -> dict[str, str]:
    return {
        "preflight.py": "\n".join(
            [
                "def can_enter(",
                "    ticker: str,",
                "    broker_position_qty: float,",
                "    buying_power: float,",
                "    required_cash: float,",
                "    *,",
                "    broker_timeout: bool = False,",
                ") -> bool:",
                "    if broker_position_qty > 0:",
                "        return False",
                "    if buying_power < required_cash:",
                "        return False",
                "    return True",
                "",
            ]
        ),
        "test_preflight.py": "\n".join(
            [
                "from preflight import can_enter",
                "",
                "",
                "def test_duplicate_broker_position_blocks_entry():",
                "    assert not can_enter('BTC-USD', 0.25, 1000, 100)",
                "",
                "",
                "def test_insufficient_cash_blocks_entry():",
                "    assert not can_enter('BTC-USD', 0, 50, 100)",
                "",
                "",
                "def test_broker_timeout_fails_closed():",
                "    assert not can_enter('BTC-USD', 0, 1000, 100, broker_timeout=True)",
                "",
                "",
                "def test_clean_preflight_allows_entry():",
                "    assert can_enter('BTC-USD', 0, 1000, 100)",
                "",
            ]
        ),
    }


PREFLIGHT_CORRECT_PATCH = """diff --git a/preflight.py b/preflight.py
--- a/preflight.py
+++ b/preflight.py
@@ -6,6 +6,8 @@ def can_enter(
     *,
     broker_timeout: bool = False,
 ) -> bool:
+    if broker_timeout:
+        return False
     if broker_position_qty > 0:
         return False
     if buying_power < required_cash:
"""


PREFLIGHT_PARTIAL_PATCH = """diff --git a/preflight.py b/preflight.py
--- a/preflight.py
+++ b/preflight.py
@@ -8,6 +8,8 @@ def can_enter(
 ) -> bool:
     if broker_position_qty > 0:
         return False
+    if required_cash <= 0:
+        return False
     if buying_power < required_cash:
         return False
     return True
"""


def _candidate(
    *,
    candidate_id: str,
    patch: str,
    planned_file: str,
    command: tuple[str, ...],
    duration_seconds: float = 10.0,
    cost_units: float = 10.0,
    declared_commands: tuple[str, ...] | None = None,
    expected_changed_files: tuple[str, ...] | None = None,
) -> PatchCandidate:
    return PatchCandidate(
        candidate_id,
        patch,
        planned_file,
        expected_changed_files or (planned_file,),
        declared_commands if declared_commands is not None else (_command_text(command),),
        duration_seconds=duration_seconds,
        cost_units=cost_units,
    )


def default_cases() -> list[BakeoffCase]:
    python = sys.executable
    runtime_command = (python, "-m", "pytest", "test_autopilot_prompt.py", "-q")
    startup_command = (python, "-m", "pytest", "test_startup_contracts.py", "-q")
    preflight_command = (python, "-m", "pytest", "test_preflight.py", "-q")

    runtime_incumbent = _candidate(
        candidate_id="chili-runtime-quarantine",
        patch=RUNTIME_CONTROL_CORRECT_PATCH,
        planned_file="autopilot_prompt.py",
        command=runtime_command,
        duration_seconds=10.0,
        cost_units=10.0,
    )
    startup_incumbent = _candidate(
        candidate_id="chili-startup-static-contract",
        patch=STARTUP_CONTRACT_CORRECT_PATCH,
        planned_file="startup_contracts.py",
        command=startup_command,
        duration_seconds=10.0,
        cost_units=10.0,
    )
    preflight_incumbent = _candidate(
        candidate_id="chili-broker-timeout-preflight",
        patch=PREFLIGHT_CORRECT_PATCH,
        planned_file="preflight.py",
        command=preflight_command,
        duration_seconds=10.0,
        cost_units=10.0,
    )

    return [
        BakeoffCase(
            "real-chili-preflight-candidate-wins",
            "strict_candidate_win",
            _preflight_files(),
            preflight_command,
            preflight_incumbent,
            dataclasses.replace(
                preflight_incumbent,
                candidate_id="candidate-faster-preflight",
                duration_seconds=8.0,
                cost_units=8.5,
            ),
            "challenger",
            "strict_challenger_win",
        ),
        BakeoffCase(
            "real-chili-runtime-control-partial-loses",
            "runtime_control_behavior_regression",
            _runtime_control_files(),
            runtime_command,
            runtime_incumbent,
            dataclasses.replace(
                runtime_incumbent,
                candidate_id="candidate-blocks-deploy-only",
                patch=RUNTIME_CONTROL_PARTIAL_PATCH,
                duration_seconds=7.0,
                cost_units=7.0,
            ),
            "incumbent",
            "behavior_tests_failed",
        ),
        BakeoffCase(
            "real-chili-startup-static-partial-loses",
            "startup_contract_behavior_regression",
            _startup_contract_files(),
            startup_command,
            startup_incumbent,
            dataclasses.replace(
                startup_incumbent,
                candidate_id="candidate-adds-wrong-static-path",
                patch=STARTUP_CONTRACT_PARTIAL_PATCH,
                duration_seconds=7.0,
                cost_units=7.0,
            ),
            "incumbent",
            "behavior_tests_failed",
        ),
        BakeoffCase(
            "real-chili-broker-timeout-partial-loses",
            "preflight_behavior_regression",
            _preflight_files(),
            preflight_command,
            preflight_incumbent,
            dataclasses.replace(
                preflight_incumbent,
                candidate_id="candidate-ignores-timeout",
                patch=PREFLIGHT_PARTIAL_PATCH,
                duration_seconds=7.0,
                cost_units=7.0,
            ),
            "incumbent",
            "behavior_tests_failed",
        ),
        BakeoffCase(
            "real-chili-runtime-control-no-evidence-loses",
            "evidence_regression",
            _runtime_control_files(),
            runtime_command,
            runtime_incumbent,
            dataclasses.replace(
                runtime_incumbent,
                candidate_id="candidate-no-runtime-evidence",
                declared_commands=(),
                duration_seconds=6.0,
                cost_units=6.0,
            ),
            "incumbent",
            "missing_behavior_evidence",
        ),
        BakeoffCase(
            "real-chili-runtime-control-unscoped-loses",
            "scope_regression",
            _runtime_control_files(),
            runtime_command,
            runtime_incumbent,
            dataclasses.replace(
                runtime_incumbent,
                candidate_id="candidate-unscoped-runtime-change",
                patch=RUNTIME_CONTROL_UNSCOPED_PATCH,
                expected_changed_files=("autopilot_prompt.py",),
                duration_seconds=7.0,
                cost_units=7.0,
            ),
            "incumbent",
            "unscoped_patch",
        ),
    ]


def average_score(results: Sequence[BakeoffDecision]) -> int:
    if not results:
        return 0
    return round(sum(result.score for result in results) / len(results))


def missing_comparison_classes(cases: Sequence[BakeoffCase]) -> list[str]:
    covered = {case.bakeoff_class for case in cases}
    return [
        comparison_class
        for comparison_class in REQUIRED_COMPARISON_CLASSES
        if comparison_class not in covered
    ]


def benchmark_status(results: Sequence[BakeoffDecision], cases: Sequence[BakeoffCase]) -> str:
    if (
        len(results) >= MIN_CASES
        and average_score(results) >= TARGET_SCORE
        and all(result.passed for result in results)
        and not missing_comparison_classes(cases)
    ):
        return "passed"
    return "failed"


def render_scorecard(
    cases: Sequence[BakeoffCase],
    results: Sequence[BakeoffDecision],
    *,
    generated_at: datetime | None = None,
) -> str:
    generated_at = generated_at or datetime.now(timezone.utc)
    lines = [
        "# CHILI Real Candidate Bakeoff Benchmark",
        "",
        f"- Schema: {REAL_CHILI_CANDIDATE_BAKEOFF_SCHEMA_VERSION}",
        f"- Generated UTC: {generated_at.isoformat().replace('+00:00', 'Z')}",
        f"- Status: {benchmark_status(results, cases)}",
        f"- Target score: {TARGET_SCORE}",
        f"- Cases: {len(results)}",
        f"- Average score: {average_score(results)}/100",
        f"- Required comparison classes: {', '.join(REQUIRED_COMPARISON_CLASSES)}",
        "- Required behavior: candidate model/tool outputs must beat or preserve incumbent behavior on CHILI-derived bug slices before promotion.",
        "- Safety: temporary repo patch replay only; no model calls, git action in the real checkout, runtime restart, deployment, database migration, broker call, or live-trading action.",
        "",
        "| Case | Comparison Class | Decision | Reason | Score | Evidence |",
        "| --- | --- | --- | --- | ---: | --- |",
    ]
    for case, result in zip(cases, results):
        lines.append(
            "| "
            + " | ".join(
                [
                    _escape_cell(case.case_id),
                    _escape_cell(case.bakeoff_class),
                    _escape_cell(result.decision),
                    _escape_cell(result.reason),
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


def run_real_chili_candidate_bakeoff(
    *,
    output_path: Path = DEFAULT_OUTPUT,
    write: bool = True,
) -> tuple[list[BakeoffCase], list[BakeoffDecision], str, Path]:
    cases = default_cases()
    results = [decide_bakeoff(case) for case in cases]
    markdown = render_scorecard(cases, results)
    if write:
        write_scorecard(markdown, output_path)
    return cases, results, markdown, output_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay real CHILI-derived candidate patch bakeoffs.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    cases, results, markdown, output_path = run_real_chili_candidate_bakeoff(
        output_path=args.output,
        write=not args.no_write,
    )
    status = benchmark_status(results, cases)
    if args.json:
        print(
            json.dumps(
                {
                    "schema": REAL_CHILI_CANDIDATE_BAKEOFF_SCHEMA_VERSION,
                    "status": status,
                    "target_score": TARGET_SCORE,
                    "cases": [
                        {
                            "case_id": case.case_id,
                            "comparison_class": case.bakeoff_class,
                            "decision": result.decision,
                            "reason": result.reason,
                            "score": result.score,
                        }
                        for case, result in zip(cases, results)
                    ],
                },
                indent=2,
            )
        )
    else:
        print(markdown)
        if not args.no_write:
            print(f"Wrote {output_path}")
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
