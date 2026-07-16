from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "autopilot_source_churn_diagnostics.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "autopilot_source_churn_diagnostics",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Clock:
    def __init__(self, values: list[float]):
        self._values = values
        self._index = 0

    def __call__(self) -> float:
        if self._index >= len(self._values):
            return self._values[-1]
        value = self._values[self._index]
        self._index += 1
        return value


def _write_scorecard(
    path: Path,
    *,
    generated_utc: str = "2026-06-03T13:23:01Z",
    status: str = "failed",
    source_stability: str = "changed",
    source_changes: int = 12,
    source_change_preview: str = "",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "# CHILI Coding Benchmark Scorecard",
                "",
                "- Schema: chili.coding-benchmark.v1",
                "- Profile: frontier",
                f"- Generated UTC: {generated_utc}",
                f"- Status: {status}",
                "- Overall score: 100/100",
                f"- Source stability: {source_stability}",
                f"- Source changes during run: {source_changes}",
                f"- Source change preview: {source_change_preview or 'none'}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_diagnostics_reports_ready_rerun_after_stale_scorecard_quiet_watch(tmp_path):
    diagnostics = _load_module()
    changed_file = tmp_path / "app" / "services" / "current_change.py"
    changed_file.parent.mkdir(parents=True)
    changed_file.write_text("VALUE = 1\n", encoding="utf-8")
    changed_at = datetime(2026, 6, 3, 13, 40, 23, tzinfo=timezone.utc).timestamp()
    changed_file.touch()
    changed_file_stat = (changed_at, changed_at)
    import os

    os.utime(changed_file, changed_file_stat)
    scorecard = tmp_path / "project_ws" / "AgentOps" / "CODING_BENCHMARK_SCORECARD.md"
    output = tmp_path / "project_ws" / "AgentOps" / "SOURCE_CHURN_DIAGNOSTICS.md"
    _write_scorecard(scorecard)

    markdown, payload, written_path = diagnostics.run_diagnostics(
        repo_root=tmp_path,
        scorecard_path=scorecard,
        lease_path=tmp_path / "project_ws" / "AgentOps" / "missing-lease.json",
        output_path=output,
        watch_seconds=1.0,
        poll_seconds=1.0,
        snapshot_fn=diagnostics.source_tree_snapshot,
        sleep_fn=lambda _seconds: None,
        clock_fn=_Clock([0.0, 0.0, 2.0]),
        now_fn=lambda: datetime(2026, 6, 3, 13, 42, 27, tzinfo=timezone.utc),
    )

    assert written_path == output
    assert output.read_text(encoding="utf-8") == markdown
    assert payload["status"] == "warning"
    assert payload["promotion_impact"] == "blocked"
    assert payload["rerun_readiness"] == "ready_for_benchmark_rerun"
    assert payload["current_source_freshness"] == "stale"
    assert payload["source_changes_after_scorecard"] == 1
    assert payload["watch_status"] == "stable"
    assert payload["source_changes_during_watch"] == 0
    assert "app/services/current_change.py" in markdown
    assert "The tree was quiet during this diagnostic window" in markdown
    assert payload["benchmark_lease_status"] == "missing"


def test_diagnostics_detects_source_changes_during_watch(tmp_path):
    diagnostics = _load_module()
    scorecard = tmp_path / "project_ws" / "AgentOps" / "CODING_BENCHMARK_SCORECARD.md"
    output = tmp_path / "project_ws" / "AgentOps" / "SOURCE_CHURN_DIAGNOSTICS.md"
    _write_scorecard(scorecard, status="passed", source_stability="stable", source_changes=0)
    before = {
        "app/services/changing.py": diagnostics.SourceFileStat(
            path="app/services/changing.py",
            modified_ns=1_717_421_000_000_000_000,
            size=10,
        )
    }
    after = {
        "app/services/changing.py": diagnostics.SourceFileStat(
            path="app/services/changing.py",
            modified_ns=1_717_421_100_000_000_000,
            size=18,
        )
    }
    snapshots = [before, before, after]

    def snapshot_fn(_repo_root):
        if snapshots:
            return snapshots.pop(0)
        return after

    markdown, payload, _ = diagnostics.run_diagnostics(
        repo_root=tmp_path,
        scorecard_path=scorecard,
        lease_path=tmp_path / "project_ws" / "AgentOps" / "missing-lease.json",
        output_path=output,
        watch_seconds=1.0,
        poll_seconds=1.0,
        snapshot_fn=snapshot_fn,
        sleep_fn=lambda _seconds: None,
        clock_fn=_Clock([0.0, 0.0, 2.0]),
        now_fn=lambda: datetime(2026, 6, 3, 13, 42, 27, tzinfo=timezone.utc),
    )

    assert payload["status"] == "warning"
    assert payload["rerun_readiness"] == "wait_for_source_quiet"
    assert payload["watch_status"] == "changed"
    assert payload["source_changes_during_watch"] == 1
    assert payload["files_changed_during_watch"] == ["app/services/changing.py"]
    assert "| app/services/changing.py | modified |" in markdown
    assert "Source/test files changed during this diagnostic window" in markdown


def test_diagnostics_surfaces_scorecard_change_preview_and_lease_context(tmp_path):
    diagnostics = _load_module()
    scorecard = tmp_path / "project_ws" / "AgentOps" / "CODING_BENCHMARK_SCORECARD.md"
    output = tmp_path / "project_ws" / "AgentOps" / "SOURCE_CHURN_DIAGNOSTICS.md"
    lease = tmp_path / "project_ws" / "AgentOps" / "SOURCE_QUIET_BENCHMARK_LEASE.json"
    _write_scorecard(
        scorecard,
        generated_utc="2026-06-03T13:23:01Z",
        status="failed",
        source_stability="changed",
        source_changes=3,
        source_change_preview="scripts/audit_ross_visual_evidence.py, tests/test_ross_visual_evidence_audit.py",
    )
    lease.parent.mkdir(parents=True, exist_ok=True)
    lease.write_text(
        "\n".join(
            [
                "{",
                '  "status": "released",',
                '  "lease_id": "lease-123",',
                '  "holder": "autopilot_coding_benchmark",',
                '  "released_utc": "2026-06-03T13:34:00Z"',
                "}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    markdown, payload, _ = diagnostics.run_diagnostics(
        repo_root=tmp_path,
        scorecard_path=scorecard,
        lease_path=lease,
        output_path=output,
        watch_seconds=0.0,
        now_fn=lambda: datetime(2026, 6, 3, 13, 42, 27, tzinfo=timezone.utc),
    )

    assert payload["status"] == "warning"
    assert payload["benchmark_lease_status"] == "released"
    assert payload["benchmark_lease_id"] == "lease-123"
    assert payload["scorecard_source_change_preview"].startswith("scripts/audit_ross")
    assert "Confirm peer source-writing lanes honored the quiet lease" in payload["next_action"]
    assert "- Benchmark lease id: lease-123" in markdown
    assert "- Scorecard source change preview: scripts/audit_ross_visual_evidence.py" in markdown


def test_diagnostics_handles_missing_scorecard(tmp_path):
    diagnostics = _load_module()
    output = tmp_path / "project_ws" / "AgentOps" / "SOURCE_CHURN_DIAGNOSTICS.md"

    markdown, payload, _ = diagnostics.run_diagnostics(
        repo_root=tmp_path,
        scorecard_path=tmp_path / "project_ws" / "AgentOps" / "missing.md",
        lease_path=tmp_path / "project_ws" / "AgentOps" / "missing-lease.json",
        output_path=output,
        watch_seconds=0.0,
        now_fn=lambda: datetime(2026, 6, 3, 13, 42, 27, tzinfo=timezone.utc),
    )

    assert payload["status"] == "blocked"
    assert payload["promotion_impact"] == "blocked"
    assert payload["rerun_readiness"] == "scorecard_missing"
    assert payload["scorecard_status"] == "missing"
    assert payload["current_source_freshness"] == "scorecard_missing"
    assert payload["source_changes_after_scorecard"] == 0
    assert "- Scorecard status: missing" in markdown
    assert "| none |  |  |  |" in markdown
