from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.services.project_autonomy import orchestrator


def _write_agentops_scorecard(root: Path, rel_path: str, body: str) -> None:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8", newline="\n")


def _write_promotion_ready_agentops(root: Path) -> None:
    capabilities = ", ".join(orchestrator.AGENT_CODING_BENCHMARK_REQUIRED_CAPABILITIES)
    _write_agentops_scorecard(
        root,
        orchestrator.AGENT_CODING_BENCHMARK_SCORECARD_REL_PATH,
        "\n".join(
            [
                "# CHILI Coding Benchmark Scorecard",
                "",
                "- Generated UTC: " + datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "- Status: passed",
                "- Overall score: 100/100",
                "- Scenarios: 6",
                "- Pass rate: 6/6",
                "- Source stability: stable",
                "- Source changes during run: 0",
                "- Capability coverage: " + capabilities,
            ]
        ),
    )
    _write_agentops_scorecard(
        root,
        orchestrator.AGENT_MODEL_SHADOW_EVIDENCE_SCORECARD_REL_PATH,
        "- Status: passed\n- Checks: 7\n- Evidence mode: real_manifest\n",
    )
    _write_agentops_scorecard(
        root,
        orchestrator.AGENT_MODEL_CANDIDATE_TOURNAMENT_SCORECARD_REL_PATH,
        "- Status: passed\n- Cases: 6\n- Evidence mode: real_artifacts\n",
    )
    _write_agentops_scorecard(
        root,
        orchestrator.AGENT_HOSTED_PR_REPAIR_SCORECARD_REL_PATH,
        (
            "- Status: passed\n"
            "- Checks: 18\n"
            "- Evidence mode: real_inventory\n"
            "- Missing checks: none\n"
            "- Promotion eligible: true\n"
        ),
    )
    _write_agentops_scorecard(
        root,
        orchestrator.AGENT_SYNTHETIC_REPO_REPAIR_SCORECARD_REL_PATH,
        "- Status: passed\n",
    )
    _write_agentops_scorecard(
        root,
        orchestrator.AGENT_MODEL_PROMOTION_SCORECARD_REL_PATH,
        "- Status: passed\n",
    )


def test_benchmark_scenarios_use_selected_repo_local_python(tmp_path, monkeypatch):
    from scripts import autopilot_coding_benchmark as benchmark

    selected_python = str(tmp_path / ".pytest_venv" / "Scripts" / "python.exe")
    monkeypatch.setattr(
        benchmark,
        "_benchmark_python",
        lambda repo_root=benchmark.REPO_ROOT: selected_python,
    )

    scenarios = benchmark.default_scenarios(tmp_path)

    assert scenarios
    assert all(scenario.command[0] == selected_python for scenario in scenarios)


def test_benchmark_retries_timed_out_scenario_once(tmp_path, monkeypatch):
    from scripts import autopilot_coding_benchmark as benchmark

    scenario = benchmark.BenchmarkScenario(
        scenario_id="slow-once",
        name="Slow once",
        category="quality",
        command=("python", "-c", "pass"),
        cwd=tmp_path,
        timeout_seconds=12,
        capabilities=("retry evidence",),
    )
    timeouts: list[int] = []

    def fake_run_once(attempt: benchmark.BenchmarkScenario) -> benchmark.BenchmarkResult:
        timeouts.append(attempt.timeout_seconds)
        if len(timeouts) == 1:
            return benchmark.BenchmarkResult(
                scenario=attempt,
                status="timed_out",
                exit_code=None,
                duration_seconds=12.0,
                evidence="Timed out after 12s.",
            )
        return benchmark.BenchmarkResult(
            scenario=attempt,
            status="passed",
            exit_code=0,
            duration_seconds=1.5,
            evidence="retry passed",
        )

    monkeypatch.setattr(benchmark, "_run_scenario_once", fake_run_once)

    result = benchmark.run_scenario(scenario)

    assert result.status == "passed"
    assert timeouts == [12, 72]
    assert result.duration_seconds == 13.5
    assert "First attempt timed out after 12s" in result.evidence
    assert "retry timeout=72s status=passed" in result.evidence
    assert "retry passed" in result.evidence


def test_benchmark_timeout_retry_keeps_retry_failure(tmp_path, monkeypatch):
    from scripts import autopilot_coding_benchmark as benchmark

    scenario = benchmark.BenchmarkScenario(
        scenario_id="still-fails",
        name="Still fails",
        category="quality",
        command=("python", "-c", "pass"),
        cwd=tmp_path,
        timeout_seconds=30,
        capabilities=("retry evidence",),
    )
    statuses = ["timed_out", "failed"]

    def fake_run_once(attempt: benchmark.BenchmarkScenario) -> benchmark.BenchmarkResult:
        status = statuses.pop(0)
        return benchmark.BenchmarkResult(
            scenario=attempt,
            status=status,
            exit_code=1 if status == "failed" else None,
            duration_seconds=1.0,
            evidence=f"{status} evidence",
        )

    monkeypatch.setattr(benchmark, "_run_scenario_once", fake_run_once)

    result = benchmark.run_scenario(scenario)

    assert result.status == "failed"
    assert result.exit_code == 1
    assert "retry timeout=90s status=failed" in result.evidence
    assert "failed evidence" in result.evidence


def test_source_quiet_write_preflight_blocks_unrelated_writer(tmp_path):
    from scripts import autopilot_coding_benchmark as benchmark

    lease_path = tmp_path / "SOURCE_QUIET_BENCHMARK_LEASE.json"
    now = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)
    lease_path.write_text(
        json.dumps(
            {
                "schema": benchmark.SOURCE_QUIET_LEASE_SCHEMA_VERSION,
                "lease_id": "lease-123",
                "status": "active",
                "holder": "autopilot_coding_benchmark",
                "expires_utc": (now + timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
                "permission_boundary": "pause source writes",
            }
        ),
        encoding="utf-8",
    )

    blocker = benchmark.source_quiet_write_blocker(
        lease_path,
        now=now,
        environ={},
    )

    assert "Source quiet benchmark lease is active" in blocker
    assert "lease_id=lease-123" in blocker
    assert "pause source writes" in blocker


def test_source_quiet_write_preflight_allows_lease_holder(tmp_path):
    from scripts import autopilot_coding_benchmark as benchmark

    lease_path = tmp_path / "SOURCE_QUIET_BENCHMARK_LEASE.json"
    now = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)
    lease_path.write_text(
        json.dumps(
            {
                "schema": benchmark.SOURCE_QUIET_LEASE_SCHEMA_VERSION,
                "lease_id": "lease-123",
                "status": "active",
                "holder": "autopilot_coding_benchmark",
                "expires_utc": (now + timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
            }
        ),
        encoding="utf-8",
    )

    blocker = benchmark.source_quiet_write_blocker(
        lease_path,
        now=now,
        environ={benchmark.SOURCE_QUIET_LEASE_ENV: "lease-123"},
    )

    assert blocker == ""


def test_source_quiet_write_preflight_ignores_released_or_expired_lease(tmp_path):
    from scripts import autopilot_coding_benchmark as benchmark

    lease_path = tmp_path / "SOURCE_QUIET_BENCHMARK_LEASE.json"
    now = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)
    lease_path.write_text(
        json.dumps(
            {
                "lease_id": "lease-123",
                "status": "active",
                "expires_utc": (now - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
            }
        ),
        encoding="utf-8",
    )

    assert benchmark.source_quiet_write_blocker(lease_path, now=now, environ={}) == ""

    lease_path.write_text(
        json.dumps(
            {
                "lease_id": "lease-123",
                "status": "released",
                "expires_utc": (now + timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
            }
        ),
        encoding="utf-8",
    )

    assert benchmark.source_quiet_write_blocker(lease_path, now=now, environ={}) == ""


def test_benchmark_scorecard_requires_real_model_tournament_mode(tmp_path):
    _write_promotion_ready_agentops(tmp_path)
    _write_agentops_scorecard(
        tmp_path,
        orchestrator.AGENT_MODEL_CANDIDATE_TOURNAMENT_SCORECARD_REL_PATH,
        "- Status: passed\n- Cases: 6\n- Evidence mode: self_test\n",
    )

    signal = orchestrator._agent_coding_benchmark_signal(tmp_path)

    assert signal["status"] == orchestrator.AGENT_OS_READINESS_CHECK_WARNING
    assert signal["model_tournament"]["evidence_mode"] == "self_test"
    assert "real tournament artifacts" in signal["frontier_evidence_gap_labels"]
