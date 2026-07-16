from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.autopilot_real_chili_candidate_bakeoff import (  # noqa: E402
    REQUIRED_COMPARISON_CLASSES,
    average_score,
    benchmark_status,
    missing_comparison_classes,
    run_real_chili_candidate_bakeoff,
)
from scripts.autopilot_replay_benchmark_common import emit_result  # noqa: E402


DEFAULT_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "REAL_CHILI_REGRESSION_BENCHMARK.md"
SCHEMA = "chili.real-chili-regression-replay-benchmark.v1"


def _real_chili_bakeoff_passes() -> str:
    cases, results, _, _ = run_real_chili_candidate_bakeoff(write=False)
    status = benchmark_status(results, cases)
    if status != "passed":
        raise AssertionError(f"status={status}; score={average_score(results)}")
    return f"cases={len(cases)}; score={average_score(results)}; status={status}"


def _comparison_classes_are_complete() -> str:
    cases, _, _, _ = run_real_chili_candidate_bakeoff(write=False)
    missing = missing_comparison_classes(cases)
    if missing:
        raise AssertionError("missing comparison classes: " + ", ".join(missing))
    return "classes=" + ",".join(REQUIRED_COMPARISON_CLASSES)


def main(argv: Sequence[str] | None = None) -> int:
    return emit_result(
        argv=argv,
        description="Replay real CHILI regression bug slices.",
        title="CHILI Real Regression Replay Benchmark",
        schema=SCHEMA,
        output_path=DEFAULT_OUTPUT,
        checks=(
            ("real_chili_bakeoff_passes", _real_chili_bakeoff_passes),
            ("comparison_classes_are_complete", _comparison_classes_are_complete),
        ),
        required_behavior="real CHILI regression slices must distinguish safe fixes from scope, behavior, evidence, and incumbent-trust regressions.",
        safety="temporary replay only; no git action in the real checkout, runtime restart, deployment, database, broker, or live-trading action.",
    )


if __name__ == "__main__":
    raise SystemExit(main())
