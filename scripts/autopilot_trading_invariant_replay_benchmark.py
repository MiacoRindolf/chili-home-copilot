from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.autopilot_replay_benchmark_common import emit_result, run_pytest_slice  # noqa: E402


DEFAULT_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "TRADING_INVARIANT_REPLAY_BENCHMARK.md"
SCHEMA = "chili.trading-invariant-replay-benchmark.v1"


def _triple_barrier_invariants_pass() -> str:
    return run_pytest_slice(REPO_ROOT, ("tests/test_triple_barrier.py",), timeout_seconds=120)


def main(argv: Sequence[str] | None = None) -> int:
    return emit_result(
        argv=argv,
        description="Replay executable trading invariants.",
        title="CHILI Trading Invariant Replay Benchmark",
        schema=SCHEMA,
        output_path=DEFAULT_OUTPUT,
        checks=(("triple_barrier_invariants_pass", _triple_barrier_invariants_pass),),
        required_behavior="core trading math invariants must remain executable and direction-aware before trading changes are trusted.",
        safety="focused pytest replay only; no runtime restart, deployment, database migration, broker call, or live-trading action.",
    )


if __name__ == "__main__":
    raise SystemExit(main())
