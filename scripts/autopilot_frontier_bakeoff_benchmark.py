from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.code_brain.agent import _validate_diff


DEFAULT_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "FRONTIER_BAKEOFF_BENCHMARK.md"
FRONTIER_BAKEOFF_SCHEMA_VERSION = "chili.frontier-bakeoff-benchmark.v1"
TARGET_SCORE = 100
MIN_CASES = 6
OUTPUT_SNIPPET_CHARS = 260
REQUIRED_BAKEOFF_CLASSES = (
    "strict_challenger_win",
    "behavior_regression",
    "scope_regression",
    "evidence_regression",
    "shadow_tie",
    "incumbent_untrusted",
)


@dataclasses.dataclass(frozen=True)
class PatchCandidate:
    candidate_id: str
    patch: str
    planned_file: str
    expected_changed_files: tuple[str, ...]
    declared_commands: tuple[str, ...]
    duration_seconds: float
    cost_units: float


@dataclasses.dataclass(frozen=True)
class BakeoffCase:
    case_id: str
    bakeoff_class: str
    files: Mapping[str, str]
    test_command: tuple[str, ...]
    incumbent: PatchCandidate
    challenger: PatchCandidate
    expected_decision: str
    expected_reason_fragment: str
    expect_tests_fail_before: bool = True


@dataclasses.dataclass(frozen=True)
class CandidateOutcome:
    candidate: PatchCandidate
    status: str
    score: int
    reason: str
    changed_files: tuple[str, ...] = ()
    test_output: str = ""

    @property
    def passed(self) -> bool:
        return self.status == "passed" and self.score >= TARGET_SCORE


@dataclasses.dataclass(frozen=True)
class BakeoffDecision:
    decision: str
    reason: str
    incumbent: CandidateOutcome
    challenger: CandidateOutcome
    score: int

    @property
    def passed(self) -> bool:
        return self.score >= TARGET_SCORE

    @property
    def evidence(self) -> str:
        details = [
            f"expected={self.decision}",
            f"reason={self.reason}",
            f"incumbent={self.incumbent.status}/{self.incumbent.reason}",
            f"challenger={self.challenger.status}/{self.challenger.reason}",
            f"incumbent_cost={self.incumbent.candidate.cost_units:.2f}",
            f"challenger_cost={self.challenger.candidate.cost_units:.2f}",
            f"incumbent_duration={self.incumbent.candidate.duration_seconds:.2f}s",
            f"challenger_duration={self.challenger.candidate.duration_seconds:.2f}s",
        ]
        if self.challenger.changed_files:
            details.append("challenger_changed=" + ",".join(self.challenger.changed_files))
        if self.challenger.test_output:
            details.append("test=" + _clip_output(self.challenger.test_output))
        return "; ".join(details)


def _escape_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _command_text(command: Sequence[str]) -> str:
    return " ".join(command)


def _clip_output(output: str, limit: int = OUTPUT_SNIPPET_CHARS) -> str:
    clean = " | ".join(line.strip() for line in (output or "").splitlines() if line.strip())
    if len(clean) <= limit:
        return clean or "Command completed without output."
    return clean[: limit - 3].rstrip() + "..."


def _write_repo(root: Path, files: Mapping[str, str]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _run_command(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(cwd) if not existing else str(cwd) + os.pathsep + existing
    return subprocess.run(
        list(command),
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
        check=False,
    )


def _file_snapshot(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith((".git/", ".pytest_cache/", "__pycache__/")):
            continue
        if "/__pycache__/" in rel or "/.pytest_cache/" in rel:
            continue
        out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _changed_files(before: Mapping[str, str], after: Mapping[str, str]) -> tuple[str, ...]:
    keys = sorted(set(before) | set(after))
    return tuple(path for path in keys if before.get(path) != after.get(path))


def _diff_new_paths(diff_text: str) -> tuple[str, ...]:
    paths: list[str] = []
    for line in (diff_text or "").splitlines():
        if not line.startswith("+++ "):
            continue
        raw = line[4:].strip().split("\t", 1)[0]
        if raw == "/dev/null":
            continue
        if raw.startswith("b/"):
            raw = raw[2:]
        paths.append(raw.replace("\\", "/"))
    return tuple(paths)


def _is_safe_repo_relative(path: str) -> bool:
    if not path or path == "/dev/null":
        return False
    p = Path(path)
    if p.is_absolute():
        return False
    return ".." not in p.parts


def _apply_patch(root: Path, patch: str) -> tuple[bool, str]:
    git = shutil.which("git")
    if not git:
        return False, "git is not available"
    check = subprocess.run(
        [git, "apply", "--check", "--whitespace=nowarn", "-"],
        cwd=root,
        input=patch,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if check.returncode != 0:
        return False, (check.stderr or check.stdout or "git apply --check failed").strip()
    applied = subprocess.run(
        [git, "apply", "--whitespace=nowarn", "-"],
        cwd=root,
        input=patch,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if applied.returncode != 0:
        return False, (applied.stderr or applied.stdout or "git apply failed").strip()
    return True, "applied"


def _validate_candidate_patch(case: BakeoffCase, candidate: PatchCandidate, root: Path) -> tuple[str, str]:
    required_command = _command_text(case.test_command)
    declared = {command.strip() for command in candidate.declared_commands if command.strip()}
    if required_command not in declared:
        return "missing_behavior_evidence", f"required_command={required_command}"

    targets = _diff_new_paths(candidate.patch)
    unsafe = [target for target in targets if not _is_safe_repo_relative(target)]
    if unsafe:
        return "unsafe_path", "unsafe_targets=" + ",".join(unsafe)

    expected = set(candidate.expected_changed_files)
    extra = [target for target in targets if target not in expected]
    if extra:
        return "unscoped_patch", "extra=" + ",".join(extra)

    if not candidate.planned_file:
        return "missing_plan_file", "candidate has no planned file"
    file_path = root / candidate.planned_file
    file_content = file_path.read_text(encoding="utf-8") if file_path.exists() else None
    validation = _validate_diff(
        candidate.patch,
        candidate.planned_file,
        file_content,
        allow_new_file=candidate.planned_file not in case.files,
    )
    if not validation.get("valid"):
        warnings = "; ".join(str(warning) for warning in validation.get("warnings") or [])
        return "invalid_diff", warnings or "diff validation failed"
    return "patch_valid", "targets=" + ",".join(targets)


def evaluate_candidate(case: BakeoffCase, candidate: PatchCandidate) -> CandidateOutcome:
    with tempfile.TemporaryDirectory(prefix=f"chili-bakeoff-{case.case_id}-") as tmp:
        root = Path(tmp) / "repo"
        root.mkdir()
        _write_repo(root, case.files)

        before_test = _run_command(case.test_command, root)
        if case.expect_tests_fail_before and before_test.returncode == 0:
            return CandidateOutcome(
                candidate,
                status="failed",
                score=0,
                reason="unexpected_green_before",
                test_output=before_test.stdout + before_test.stderr,
            )

        validation_status, validation_evidence = _validate_candidate_patch(case, candidate, root)
        if validation_status != "patch_valid":
            return CandidateOutcome(
                candidate,
                status="failed",
                score=0,
                reason=validation_status,
                test_output=validation_evidence,
            )

        before_snapshot = _file_snapshot(root)
        applied, apply_evidence = _apply_patch(root, candidate.patch)
        if not applied:
            return CandidateOutcome(
                candidate,
                status="failed",
                score=0,
                reason="apply_failed",
                test_output=apply_evidence,
            )

        after_snapshot = _file_snapshot(root)
        changed = _changed_files(before_snapshot, after_snapshot)
        unexpected = [path for path in changed if path not in candidate.expected_changed_files]
        if unexpected:
            return CandidateOutcome(
                candidate,
                status="failed",
                score=0,
                reason="unexpected_changed_files",
                changed_files=changed,
            )

        after_test = _run_command(case.test_command, root)
        output = after_test.stdout + after_test.stderr
        if after_test.returncode != 0:
            return CandidateOutcome(
                candidate,
                status="failed",
                score=0,
                reason="behavior_tests_failed",
                changed_files=changed,
                test_output=output,
            )

        return CandidateOutcome(
            candidate,
            status="passed",
            score=100,
            reason="behavior_tests_passed",
            changed_files=changed,
            test_output=output,
        )


def _challenger_has_measured_advantage(incumbent: CandidateOutcome, challenger: CandidateOutcome) -> bool:
    duration_win = (
        incumbent.candidate.duration_seconds > 0
        and challenger.candidate.duration_seconds <= incumbent.candidate.duration_seconds * 0.9
    )
    cost_win = challenger.candidate.cost_units <= incumbent.candidate.cost_units * 0.9
    return duration_win or cost_win


def decide_bakeoff(case: BakeoffCase) -> BakeoffDecision:
    incumbent = evaluate_candidate(case, case.incumbent)
    challenger = evaluate_candidate(case, case.challenger)

    if not incumbent.passed:
        decision = "hold"
        reason = "incumbent_untrusted"
    elif not challenger.passed:
        decision = "incumbent"
        reason = challenger.reason
    elif _challenger_has_measured_advantage(incumbent, challenger):
        decision = "challenger"
        reason = "strict_challenger_win"
    else:
        decision = "shadow"
        reason = "no_measured_improvement"

    passed = decision == case.expected_decision and case.expected_reason_fragment in reason
    return BakeoffDecision(
        decision=decision,
        reason=reason,
        incumbent=incumbent,
        challenger=challenger,
        score=100 if passed else 0,
    )


def _selector_files() -> dict[str, str]:
    return {
        "selector.py": "\n".join(
            [
                "def best_window(values: list[int]) -> int | None:",
                "    if not values:",
                "        return None",
                "    best = values[0]",
                "    for index in range(1, len(values) - 1):",
                "        if values[index] > best:",
                "            best = values[index]",
                "    return best",
                "",
            ]
        ),
        "test_selector.py": "\n".join(
            [
                "from selector import best_window",
                "",
                "",
                "def test_best_window_includes_last_value():",
                "    assert best_window([2, 4, 9]) == 9",
                "",
                "",
                "def test_best_window_empty_input():",
                "    assert best_window([]) is None",
                "",
            ]
        ),
        "README.md": "selector fixture\n",
    }


CORRECT_PATCH = """diff --git a/selector.py b/selector.py
--- a/selector.py
+++ b/selector.py
@@ -2,7 +2,7 @@ def best_window(values: list[int]) -> int | None:
     if not values:
         return None
     best = values[0]
-    for index in range(1, len(values) - 1):
+    for index in range(1, len(values)):
         if values[index] > best:
             best = values[index]
     return best
"""


PARTIAL_PATCH = """diff --git a/selector.py b/selector.py
--- a/selector.py
+++ b/selector.py
@@ -2,7 +2,7 @@ def best_window(values: list[int]) -> int | None:
     if not values:
         return None
     best = values[0]
-    for index in range(1, len(values) - 1):
+    for index in range(1, max(len(values) - 1, 1)):
         if values[index] > best:
             best = values[index]
     return best
"""


UNSCOPED_PATCH = """diff --git a/selector.py b/selector.py
--- a/selector.py
+++ b/selector.py
@@ -2,7 +2,7 @@ def best_window(values: list[int]) -> int | None:
     if not values:
         return None
     best = values[0]
-    for index in range(1, len(values) - 1):
+    for index in range(1, len(values)):
         if values[index] > best:
             best = values[index]
     return best
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1,2 @@
 selector fixture
+unrelated note
"""


def default_cases() -> list[BakeoffCase]:
    python = sys.executable
    test_command = (python, "-m", "pytest", "test_selector.py", "-q")
    command = _command_text(test_command)
    incumbent = PatchCandidate(
        "chili-incumbent",
        CORRECT_PATCH,
        "selector.py",
        ("selector.py",),
        (command,),
        duration_seconds=10.0,
        cost_units=10.0,
    )
    fast_challenger = dataclasses.replace(
        incumbent,
        candidate_id="challenger-fast-correct",
        duration_seconds=8.0,
        cost_units=8.5,
    )
    partial_challenger = dataclasses.replace(
        incumbent,
        candidate_id="challenger-partial",
        patch=PARTIAL_PATCH,
        duration_seconds=7.0,
        cost_units=7.0,
    )
    unscoped_challenger = dataclasses.replace(
        incumbent,
        candidate_id="challenger-unscoped",
        patch=UNSCOPED_PATCH,
        duration_seconds=7.0,
        cost_units=7.0,
    )
    no_evidence_challenger = dataclasses.replace(
        incumbent,
        candidate_id="challenger-no-evidence",
        declared_commands=(),
        duration_seconds=6.0,
        cost_units=6.0,
    )
    tie_challenger = dataclasses.replace(
        incumbent,
        candidate_id="challenger-tie",
        duration_seconds=10.0,
        cost_units=10.0,
    )
    untrusted_incumbent = dataclasses.replace(
        incumbent,
        candidate_id="chili-incumbent-untrusted",
        patch=PARTIAL_PATCH,
    )
    return [
        BakeoffCase(
            "correct-challenger-beats-incumbent",
            "strict_challenger_win",
            _selector_files(),
            test_command,
            incumbent,
            fast_challenger,
            "challenger",
            "strict_challenger_win",
        ),
        BakeoffCase(
            "partial-frontier-patch-loses",
            "behavior_regression",
            _selector_files(),
            test_command,
            incumbent,
            partial_challenger,
            "incumbent",
            "behavior_tests_failed",
        ),
        BakeoffCase(
            "unscoped-frontier-patch-loses",
            "scope_regression",
            _selector_files(),
            test_command,
            incumbent,
            unscoped_challenger,
            "incumbent",
            "unscoped_patch",
        ),
        BakeoffCase(
            "no-evidence-frontier-patch-loses",
            "evidence_regression",
            _selector_files(),
            test_command,
            incumbent,
            no_evidence_challenger,
            "incumbent",
            "missing_behavior_evidence",
        ),
        BakeoffCase(
            "equal-frontier-patch-remains-shadow",
            "shadow_tie",
            _selector_files(),
            test_command,
            incumbent,
            tie_challenger,
            "shadow",
            "no_measured_improvement",
        ),
        BakeoffCase(
            "untrusted-incumbent-holds-bakeoff",
            "incumbent_untrusted",
            _selector_files(),
            test_command,
            untrusted_incumbent,
            fast_challenger,
            "hold",
            "incumbent_untrusted",
        ),
    ]


def average_score(results: Sequence[BakeoffDecision]) -> int:
    if not results:
        return 0
    return round(sum(result.score for result in results) / len(results))


def missing_bakeoff_classes(results: Sequence[BakeoffDecision], cases: Sequence[BakeoffCase]) -> list[str]:
    covered = {case.bakeoff_class for case in cases}
    return [
        bakeoff_class
        for bakeoff_class in REQUIRED_BAKEOFF_CLASSES
        if bakeoff_class not in covered
    ]


def benchmark_status(results: Sequence[BakeoffDecision], cases: Sequence[BakeoffCase]) -> str:
    if (
        len(results) >= MIN_CASES
        and average_score(results) >= TARGET_SCORE
        and all(result.passed for result in results)
        and not missing_bakeoff_classes(results, cases)
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
        "# CHILI Frontier Bakeoff Benchmark",
        "",
        f"- Schema: {FRONTIER_BAKEOFF_SCHEMA_VERSION}",
        f"- Generated UTC: {generated_at.isoformat().replace('+00:00', 'Z')}",
        f"- Status: {benchmark_status(results, cases)}",
        f"- Target score: {TARGET_SCORE}",
        f"- Cases: {len(results)}",
        f"- Average score: {average_score(results)}/100",
        f"- Required bakeoff classes: {', '.join(REQUIRED_BAKEOFF_CLASSES)}",
        "- Required behavior: challenger model/tool patches must beat the incumbent on scoped behavior-tested outcomes, not just plausible diffs or green-looking claims.",
        "- Safety: temporary repo patch replay only; no model calls, git action in the real checkout, runtime restart, deployment, database migration, broker call, or live-trading action.",
        "",
        "| Case | Bakeoff Class | Decision | Reason | Score | Evidence |",
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


def run_frontier_bakeoff_benchmark(
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
    parser = argparse.ArgumentParser(description="Replay CHILI frontier patch bakeoff decisions.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--json", action="store_true", help="Print a JSON summary instead of markdown.")
    parser.add_argument("--no-write", action="store_true", help="Do not write the scorecard file.")
    args = parser.parse_args(argv)

    cases, results, markdown, output_path = run_frontier_bakeoff_benchmark(
        output_path=args.output,
        write=not args.no_write,
    )
    status = benchmark_status(results, cases)
    if args.json:
        print(
            json.dumps(
                {
                    "schema": FRONTIER_BAKEOFF_SCHEMA_VERSION,
                    "status": status,
                    "average_score": average_score(results),
                    "cases": len(results),
                    "output": str(output_path),
                    "results": [
                        {
                            "case_id": case.case_id,
                            "bakeoff_class": case.bakeoff_class,
                            "decision": result.decision,
                            "reason": result.reason,
                            "score": result.score,
                            "evidence": result.evidence,
                        }
                        for case, result in zip(cases, results)
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(markdown)
        if not args.no_write:
            print(f"Wrote {output_path}")
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
