from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.autopilot_replay_benchmark_common import emit_result, run_pytest_slice  # noqa: E402


DEFAULT_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "TRADING_DB_INCIDENT_REPLAY_BENCHMARK.md"
SCHEMA = "chili.trading-db-incident-replay-benchmark.v1"
TRANSIENT_DB_SETUP_CONTEXT_MARKERS = (
    "error at setup",
    "engine.raw_connection",
    "raw_connection()",
    "sqlalchemy.engine.base.py:143",
)
TRANSIENT_DB_CONNECTION_MARKERS = (
    "connection refused",
    "connection timed out",
    "could not connect",
    "remaining connection slots",
    "server closed the connection",
    "too many clients",
    "winerror 10055",
    "psycopg2.operationalerror",
    "sqlalchemy.exc.operationalerror",
)


def _is_transient_db_setup_failure(evidence: str) -> bool:
    normalized = evidence.lower()
    return any(marker in normalized for marker in TRANSIENT_DB_SETUP_CONTEXT_MARKERS) and any(
        marker in normalized for marker in TRANSIENT_DB_CONNECTION_MARKERS
    )


def _compact_retry_evidence(evidence: str, *, max_chars: int = 360) -> str:
    compact = " ".join(line.strip() for line in evidence.splitlines() if line.strip())
    return compact[:max_chars]


def _run_db_pytest_slice(tests: Sequence[str], *, timeout_seconds: int = 120) -> str:
    first_transient_failure = ""
    for attempt in range(1, 3):
        try:
            evidence = run_pytest_slice(REPO_ROOT, tests, timeout_seconds=timeout_seconds)
        except AssertionError as exc:
            failure = str(exc)
            if attempt == 1 and _is_transient_db_setup_failure(failure):
                first_transient_failure = failure
                continue
            raise
        if first_transient_failure:
            return (
                f"{evidence} | transient_db_setup_retry_attempts=2; "
                f"first_failure={_compact_retry_evidence(first_transient_failure)}"
            )
        return evidence
    raise AssertionError("transient DB setup retry exhausted")


def _queue_pressure_nonplacement_audit_passes() -> str:
    return _run_db_pytest_slice(
        (
            "tests/test_auto_trader_safety.py::test_queue_pressure_non_positive_edge_audit_drops_before_commit",
            "tests/test_auto_trader_safety.py::test_queue_pressure_regime_gate_audit_drops_before_commit",
            "tests/test_auto_trader_safety.py::test_cost_gate_repeat_pressure_audit_drops_before_commit",
            "tests/test_auto_trader_safety.py::test_placement_audit_with_queue_pressure_snapshot_still_writes",
        ),
        timeout_seconds=120,
    )


def _candidate_selector_scope_lane_passes() -> str:
    return _run_db_pytest_slice(
        ("tests/test_auto_trader_safety.py::test_candidate_selector_splits_user_and_system_scope_lanes",),
        timeout_seconds=120,
    )


def main(argv: Sequence[str] | None = None) -> int:
    return emit_result(
        argv=argv,
        description="Replay DB-backed trading incidents.",
        title="CHILI DB Incident Replay Benchmark",
        schema=SCHEMA,
        output_path=DEFAULT_OUTPUT,
        checks=(
            ("queue_pressure_nonplacement_audit_passes", _queue_pressure_nonplacement_audit_passes),
            ("candidate_selector_scope_lane_passes", _candidate_selector_scope_lane_passes),
        ),
        required_behavior="DB-backed incident replays must preserve queue-pressure audit shedding and split candidate scope lanes without loosening placement audits.",
        safety="focused pytest replay only; no runtime restart, deployment, database migration, broker call, or live-trading action.",
    )


if __name__ == "__main__":
    raise SystemExit(main())
