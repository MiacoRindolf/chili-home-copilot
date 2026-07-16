from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.autopilot_model_candidate_artifact_builder import (  # noqa: E402
    ArtifactBuildError,
    build_artifact,
    synthetic_drops,
)
from scripts.autopilot_model_candidate_drop_collector import run_self_test  # noqa: E402
from scripts.autopilot_replay_benchmark_common import emit_result  # noqa: E402


DEFAULT_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "MODEL_CANDIDATE_PROVENANCE_BENCHMARK.md"
SCHEMA = "chili.model-candidate-provenance-benchmark.v1"


def _collector_stamps_verified_provenance() -> str:
    summary = run_self_test()
    if summary.get("validated_with_provenance") is not True:
        raise AssertionError("collector did not validate provenance")
    return f"collector={summary['collector']}; cases={summary['cases']}; artifact={summary['artifact_schema']}"


def _artifact_builder_rejects_missing_provenance() -> str:
    drop = dict(synthetic_drops()[0])
    drop.pop("provenance", None)
    try:
        build_artifact([drop], allow_partial=True, require_provenance=True)
    except ArtifactBuildError as exc:
        if "provenance is required" not in str(exc):
            raise
        return str(exc)
    raise AssertionError("artifact builder accepted a drop without required provenance")


def main(argv: Sequence[str] | None = None) -> int:
    return emit_result(
        argv=argv,
        description="Replay the model candidate provenance gate.",
        title="CHILI Model Candidate Provenance Benchmark",
        schema=SCHEMA,
        output_path=DEFAULT_OUTPUT,
        checks=(
            ("collector_stamps_verified_provenance", _collector_stamps_verified_provenance),
            ("artifact_builder_rejects_missing_provenance", _artifact_builder_rejects_missing_provenance),
        ),
        required_behavior="candidate drops must carry prompt-pack, transcript, collector, and run provenance before promotion evidence can use them.",
        safety="local fixture/hash validation only; no model calls, git action, runtime restart, deployment, database, broker, or live-trading action.",
    )


if __name__ == "__main__":
    raise SystemExit(main())
