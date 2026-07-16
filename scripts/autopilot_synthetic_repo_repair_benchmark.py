from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.autopilot_code_agent_unit_benchmark import render_payload, run_suite  # noqa: E402
from scripts.autopilot_replay_benchmark_common import emit_result  # noqa: E402


DEFAULT_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "SYNTHETIC_REPO_REPAIR_BENCHMARK.md"
SCHEMA = "chili.synthetic-repo-repair-benchmark.v1"


def _suite_passes(suite: str) -> str:
    payload = render_payload(suite, run_suite(suite))
    if payload["status"] != "passed":
        raise AssertionError(payload)
    return f"suite={suite}; checks={payload['checks']}; passed={payload['passed']}"


def main(argv: Sequence[str] | None = None) -> int:
    return emit_result(
        argv=argv,
        description="Replay synthetic repo repair gates.",
        title="CHILI Synthetic Repo Repair Replay Benchmark",
        schema=SCHEMA,
        output_path=DEFAULT_OUTPUT,
        checks=(
            ("plan_safety_replay", lambda: _suite_passes("plan-safety")),
            ("request_preflight_replay", lambda: _suite_passes("request-preflight-safety")),
            ("diff_safety_replay", lambda: _suite_passes("diff-safety")),
        ),
        required_behavior="synthetic repair prompts must preserve clarification, destructive preflight, and side-effect diff gates.",
        safety="in-memory/unit replay only; no git action, source mutation, runtime restart, deployment, database, broker, or live-trading action.",
    )


if __name__ == "__main__":
    raise SystemExit(main())
