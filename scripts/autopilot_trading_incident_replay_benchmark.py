from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.autopilot_replay_benchmark_common import emit_result, run_pytest_slice  # noqa: E402


DEFAULT_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "TRADING_INCIDENT_REPLAY_BENCHMARK.md"
SCHEMA = "chili.trading-incident-replay-benchmark.v1"


def _paper_entry_direction_incident_passes() -> str:
    return run_pytest_slice(
        REPO_ROOT,
        (
            "tests/test_paper_trading_options.py::test_auto_enter_stock_short_signal_preserves_directional_geometry",
            "tests/test_paper_trading_options.py::test_auto_enter_detailed_reports_same_direction_duplicate_open_block",
            "tests/test_paper_trading_options.py::test_auto_enter_detailed_reports_opposite_direction_duplicate_open_block",
        ),
        timeout_seconds=120,
    )


def _malformed_auto_entry_cleanup_passes() -> str:
    return run_pytest_slice(
        REPO_ROOT,
        ("tests/test_paper_trading_options.py::test_check_paper_exits_cancels_invalid_auto_entry_geometry",),
        timeout_seconds=120,
    )


def main(argv: Sequence[str] | None = None) -> int:
    return emit_result(
        argv=argv,
        description="Replay multi-step trading incidents.",
        title="CHILI Trading Incident Replay Benchmark",
        schema=SCHEMA,
        output_path=DEFAULT_OUTPUT,
        checks=(
            ("paper_entry_direction_incident_passes", _paper_entry_direction_incident_passes),
            ("malformed_auto_entry_cleanup_passes", _malformed_auto_entry_cleanup_passes),
        ),
        required_behavior="paper-entry incident replays must preserve short-side geometry, duplicate diagnostics, and malformed auto-entry cancellation.",
        safety="focused pytest replay only; no runtime restart, deployment, database migration, broker call, or live-trading action.",
    )


if __name__ == "__main__":
    raise SystemExit(main())
