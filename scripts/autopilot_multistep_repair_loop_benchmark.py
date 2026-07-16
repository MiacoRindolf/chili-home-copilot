from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.autopilot_replay_benchmark_common import emit_result  # noqa: E402


DEFAULT_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "MULTISTEP_REPAIR_LOOP_BENCHMARK.md"
SCHEMA = "chili.multistep-repair-loop-benchmark.v1"


def _run_pytest_slice(*tests: str) -> str:
    command = [sys.executable, "-m", "pytest", *tests, "-q"]
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    output = " | ".join(
        line.strip()
        for line in (completed.stdout + "\n" + completed.stderr).splitlines()
        if line.strip()
    )
    if completed.returncode != 0:
        raise AssertionError(output[:1000])
    return output[:700]


def _execution_loop_worktree_repair_passes() -> str:
    return _run_pytest_slice("tests/test_coding_execution_loop_llm_cost.py")


def _validation_context_repair_passes() -> str:
    return _run_pytest_slice(
        "tests/test_project_autonomy_service.py::test_coding_benchmark_signal_treats_repaired_replay_rows_as_clean",
        "tests/test_project_autonomy_service.py::test_agent_os_readiness_operator_inbox_names_goal_receipt_quality",
    )


def main(argv: Sequence[str] | None = None) -> int:
    return emit_result(
        argv=argv,
        description="Replay multi-step repair loop evidence.",
        title="CHILI Multi-Step Repair Loop Benchmark",
        schema=SCHEMA,
        output_path=DEFAULT_OUTPUT,
        checks=(
            ("execution_loop_worktree_repair_passes", _execution_loop_worktree_repair_passes),
            ("validation_context_repair_passes", _validation_context_repair_passes),
        ),
        required_behavior="repair loops must isolate worktrees, preserve handoff packets, and treat repaired replay evidence as clean only after validation.",
        safety="focused pytest replay only; no git action in the real checkout, runtime restart, deployment, database, broker, or live-trading action.",
    )


if __name__ == "__main__":
    raise SystemExit(main())
