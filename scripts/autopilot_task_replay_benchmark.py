from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.autopilot_archived_task_replay_benchmark import grade_archived_task  # noqa: E402
from scripts.autopilot_report_replay_benchmark import grade_report  # noqa: E402
from scripts.autopilot_replay_benchmark_common import emit_result  # noqa: E402


DEFAULT_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "TASK_REPLAY_BENCHMARK.md"
SCHEMA = "chili.task-replay-benchmark.v1"


GOOD_REPORT = """# Trading Repair Handoff

- Generated UTC: 2026-06-03T12:00:00Z
- Status: completed

## Scope
Review PR #282 hosted CI repair evidence for app/services/trading/pattern_imminent_alerts.py.

## Evidence
- Command: python -m pytest tests/test_pattern_imminent_alerts.py -q
- Result: passed
- SHA256: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
- Published artifact: project_ws/AgentOps/PR_282_CI_REPAIR.md

## Decision
Ready for operator review after hosted check success is bound to the current head.

## Safety Boundary
No source/test/git/PR/runtime/docker/database/broker/live-trading action was performed or authorized by this report.
"""


BAD_REPORT = """# Thin Task Note

- Generated UTC: 2026-06-03T12:00:00Z

## Scope
Looked at the queue.

## Decision
Probably fine.
"""


def _good_task_report_scores_as_passed() -> str:
    with tempfile.TemporaryDirectory(prefix="chili_task_replay_") as tmp:
        root = Path(tmp)
        path = root / "task.md"
        path.write_text(GOOD_REPORT, encoding="utf-8")
        report_grade = grade_report(path, root=root, recognize_repairs=False)
        archived_grade = grade_archived_task(path, root=root, recognize_repairs=False)
    if report_grade.score < 85 or archived_grade.score < 85:
        raise AssertionError(f"report={report_grade}; archived={archived_grade}")
    return f"report_score={report_grade.score}; archived_score={archived_grade.score}; classes={','.join(archived_grade.semantic_classes)}"


def _missing_safety_boundary_is_rejected() -> str:
    with tempfile.TemporaryDirectory(prefix="chili_task_replay_bad_") as tmp:
        root = Path(tmp)
        path = root / "bad_task.md"
        path.write_text(BAD_REPORT, encoding="utf-8")
        report_grade = grade_report(path, root=root, recognize_repairs=False)
        archived_grade = grade_archived_task(path, root=root, recognize_repairs=False)
    if report_grade.score >= 85 or archived_grade.score >= 85:
        raise AssertionError(f"unsafe report unexpectedly passed: {report_grade}; {archived_grade}")
    return f"report_missing={','.join(report_grade.missing)}; archived_missing={','.join(archived_grade.missing)}"


def main(argv: Sequence[str] | None = None) -> int:
    return emit_result(
        argv=argv,
        description="Replay task-level report grading.",
        title="CHILI Task Replay Benchmark",
        schema=SCHEMA,
        output_path=DEFAULT_OUTPUT,
        checks=(
            ("good_task_report_scores_as_passed", _good_task_report_scores_as_passed),
            ("missing_safety_boundary_is_rejected", _missing_safety_boundary_is_rejected),
        ),
        required_behavior="task replay grading must reward evidence-backed decisions and reject thin reports without safety boundaries.",
        safety="temporary markdown grading only; no git action, runtime restart, deployment, database, broker, or live-trading action.",
    )


if __name__ == "__main__":
    raise SystemExit(main())
