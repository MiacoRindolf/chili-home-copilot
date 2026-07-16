from __future__ import annotations

import argparse
import dataclasses
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.autopilot_frontier_bakeoff_benchmark import _escape_cell  # noqa: E402


TARGET_SCORE = 100


@dataclasses.dataclass(frozen=True)
class ReplayCheck:
    check_id: str
    evidence: str
    score: int = TARGET_SCORE

    @property
    def passed(self) -> bool:
        return self.score >= TARGET_SCORE


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def run_check(check_id: str, fn: Callable[[], str]) -> ReplayCheck:
    try:
        evidence = fn()
    except Exception as exc:  # pragma: no cover - failure path is rendered for operators.
        return ReplayCheck(check_id, f"{type(exc).__name__}: {exc}", 0)
    return ReplayCheck(check_id, evidence or "passed", TARGET_SCORE)


def run_pytest_slice(repo_root: Path, tests: Sequence[str], *, timeout_seconds: int = 120) -> str:
    command = [sys.executable, "-m", "pytest", *tests, "-q"]
    completed = subprocess.run(
        command,
        cwd=repo_root,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    output = " | ".join(
        line.strip()
        for line in (completed.stdout + "\n" + completed.stderr).splitlines()
        if line.strip()
    )
    if completed.returncode != 0:
        raise AssertionError(output[:1000] or f"pytest exited {completed.returncode}")
    return output[:700] or "pytest passed"


def average_score(results: Sequence[ReplayCheck]) -> int:
    if not results:
        return 0
    return round(sum(result.score for result in results) / len(results))


def benchmark_status(results: Sequence[ReplayCheck], *, min_checks: int = 1) -> str:
    if len(results) >= min_checks and results and all(result.passed for result in results):
        return "passed"
    return "failed"


def render_scorecard(
    *,
    title: str,
    schema: str,
    results: Sequence[ReplayCheck],
    required_behavior: str,
    safety: str,
    generated_utc: str | None = None,
) -> str:
    generated_utc = generated_utc or utc_now()
    lines = [
        f"# {title}",
        "",
        f"- Schema: {schema}",
        f"- Generated UTC: {generated_utc}",
        f"- Status: {benchmark_status(results)}",
        f"- Target score: {TARGET_SCORE}",
        f"- Checks: {len(results)}",
        f"- Average score: {average_score(results)}/100",
        f"- Required behavior: {required_behavior}",
        f"- Safety: {safety}",
        "",
        "| Check | Score | Evidence |",
        "| --- | ---: | --- |",
    ]
    for result in results:
        lines.append(
            "| "
            + " | ".join(
                [
                    _escape_cell(result.check_id),
                    str(result.score),
                    _escape_cell(result.evidence),
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def write_scorecard(markdown: str, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    return output_path


def emit_result(
    *,
    argv: Sequence[str] | None,
    description: str,
    title: str,
    schema: str,
    output_path: Path,
    checks: Sequence[tuple[str, Callable[[], str]]],
    required_behavior: str,
    safety: str,
) -> int:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--output", type=Path, default=output_path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)
    results = [run_check(check_id, fn) for check_id, fn in checks]
    markdown = render_scorecard(
        title=title,
        schema=schema,
        results=results,
        required_behavior=required_behavior,
        safety=safety,
    )
    if not args.no_write:
        write_scorecard(markdown, args.output)
    status = benchmark_status(results)
    if args.json:
        print(
            json.dumps(
                {
                    "schema": schema,
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
