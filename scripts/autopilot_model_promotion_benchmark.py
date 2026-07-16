from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.project_autonomy import orchestrator  # noqa: E402
from scripts.autopilot_replay_benchmark_common import emit_result  # noqa: E402


DEFAULT_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "MODEL_PROMOTION_REPLAY_BENCHMARK.md"
SCHEMA = "chili.model-promotion-replay-benchmark.v1"


def _write_scorecard(root: Path, rel_path: str, body: str) -> Path:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _write_promotion_ready_agentops(root: Path) -> None:
    capabilities = ", ".join(orchestrator.AGENT_CODING_BENCHMARK_REQUIRED_CAPABILITIES)
    _write_scorecard(
        root,
        orchestrator.AGENT_CODING_BENCHMARK_SCORECARD_REL_PATH,
        "\n".join(
            [
                "# CHILI Coding Benchmark Scorecard",
                "",
                "- Profile: full",
                "- Generated UTC: " + datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "- Status: passed",
                "- Selected scenarios status: passed",
                "- Overall score: 100/100",
                "- Scenarios: 12",
                "- Pass rate: 12/12",
                "- Source stability: stable",
                "- Source changes during run: 0",
                "- Capability coverage: " + capabilities,
            ]
        ),
    )
    _write_scorecard(
        root,
        orchestrator.AGENT_MODEL_SHADOW_EVIDENCE_SCORECARD_REL_PATH,
        "- Status: passed\n- Checks: 7\n- Evidence mode: real_manifest\n",
    )
    _write_scorecard(
        root,
        orchestrator.AGENT_MODEL_CANDIDATE_TOURNAMENT_SCORECARD_REL_PATH,
        "- Status: passed\n- Cases: 6\n- Evidence mode: real_artifacts\n",
    )
    _write_scorecard(
        root,
        orchestrator.AGENT_HOSTED_PR_REPAIR_SCORECARD_REL_PATH,
        "- Status: passed\n- Checks: 18\n- Evidence mode: real_inventory\n- Missing checks: none\n- Promotion eligible: true\n",
    )
    _write_scorecard(root, orchestrator.AGENT_SYNTHETIC_REPO_REPAIR_SCORECARD_REL_PATH, "- Status: passed\n")
    _write_scorecard(root, orchestrator.AGENT_MODEL_PROMOTION_SCORECARD_REL_PATH, "- Status: passed\n")


def _promotion_ready_signal_passes() -> str:
    with tempfile.TemporaryDirectory(prefix="chili_model_promotion_") as tmp:
        root = Path(tmp)
        _write_promotion_ready_agentops(root)
        signal = orchestrator._agent_coding_benchmark_signal(root)
    if signal["status"] != orchestrator.AGENT_OS_READINESS_CHECK_PASSED:
        raise AssertionError(signal["detail"])
    return f"promotion_status={signal['promotion_status']}; score={signal['score']}; scope={signal['promotion_scope']}"


def _missing_promotion_scorecard_blocks() -> str:
    with tempfile.TemporaryDirectory(prefix="chili_model_promotion_missing_") as tmp:
        root = Path(tmp)
        _write_promotion_ready_agentops(root)
        (root / orchestrator.AGENT_MODEL_PROMOTION_SCORECARD_REL_PATH).unlink()
        signal = orchestrator._agent_coding_benchmark_signal(root)
    if signal["status"] != orchestrator.AGENT_OS_READINESS_CHECK_WARNING:
        raise AssertionError("missing model promotion scorecard did not block")
    if orchestrator.AGENT_MODEL_PROMOTION_SCORECARD_REL_PATH not in signal["detail"]:
        raise AssertionError(signal["detail"])
    return signal["detail"]


def _self_test_frontier_evidence_blocks() -> str:
    with tempfile.TemporaryDirectory(prefix="chili_model_promotion_selftest_") as tmp:
        root = Path(tmp)
        _write_promotion_ready_agentops(root)
        _write_scorecard(
            root,
            orchestrator.AGENT_HOSTED_PR_REPAIR_SCORECARD_REL_PATH,
            "- Status: passed\n- Checks: 18\n- Evidence mode: self_test\n- Promotion eligible: false\n",
        )
        signal = orchestrator._agent_coding_benchmark_signal(root)
    if signal["status"] != orchestrator.AGENT_OS_READINESS_CHECK_WARNING:
        raise AssertionError("self-test frontier evidence did not block")
    if "real PR repair inventory" not in signal["frontier_evidence_gap_labels"]:
        raise AssertionError(signal["frontier_evidence_gap_labels"])
    return ",".join(signal["frontier_evidence_gap_labels"])


def main(argv: Sequence[str] | None = None) -> int:
    return emit_result(
        argv=argv,
        description="Replay the model/tool promotion quality gate.",
        title="CHILI Model Promotion Replay Benchmark",
        schema=SCHEMA,
        output_path=DEFAULT_OUTPUT,
        checks=(
            ("promotion_ready_signal_passes", _promotion_ready_signal_passes),
            ("missing_promotion_scorecard_blocks", _missing_promotion_scorecard_blocks),
            ("self_test_frontier_evidence_blocks", _self_test_frontier_evidence_blocks),
        ),
        required_behavior="model/tool promotion must be gated by all-up coding score, source stability, and real shadow/tournament/hosted PR evidence.",
        safety="temporary scorecard replay only; no model calls, git action, runtime restart, deployment, database, broker, or live-trading action.",
    )


if __name__ == "__main__":
    raise SystemExit(main())
