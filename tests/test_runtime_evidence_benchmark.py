from __future__ import annotations

import inspect
import json
import os
from pathlib import Path

from scripts import autopilot_runtime_evidence_benchmark as benchmark

from app.services.project_autonomy import diagnostic_runtime_evidence


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "autonomy_runtime_diagnostics"


def test_runtime_holdout_cases_do_not_contain_oracle_labels():
    manifest = json.loads((FIXTURE_ROOT / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["reference_family"] == "claude-fable-5"
    assert len(manifest["cases"]) == 3
    for entry in manifest["cases"]:
        case = json.loads((FIXTURE_ROOT / entry["case"]).read_text(encoding="utf-8"))
        oracle = json.loads((FIXTURE_ROOT / entry["oracle"]).read_text(encoding="utf-8"))
        assert "expected_dimension" not in case
        assert "expected_probe_kinds" not in case
        assert oracle["case_id"] == case["case_id"]


def test_runtime_database_fixture_exists_only_inside_test_context():
    test_url = os.environ["TEST_DATABASE_URL"]
    specs = [{"name": "project_runtime_probe_test_events", "groups": {"stale": 3}}]
    probe = {
        "table": "project_runtime_probe_test_events",
        "timestamp_column": "created_at",
        "lookback_minutes": 60,
        "group_by": "cause",
    }

    with benchmark._database_fixture(test_url, specs):
        result = diagnostic_runtime_evidence.execute_db_profile(
            probe,
            explicit_test_url=test_url,
        )
        payload = json.loads(result["output"])
        assert result["status"] == "completed"
        assert payload["count"] == 3
        assert payload["groups"] == [{"count": 3, "value": "stale"}]

    missing = diagnostic_runtime_evidence.execute_db_schema(
        {"table": "project_runtime_probe_test_events"},
        explicit_test_url=test_url,
    )
    assert missing["status"] == "failed"
    assert "Unknown public table" in missing["output"]


def test_runtime_benchmark_scoring_requires_probe_coverage_and_retraction():
    oracle = {
        "expected_dimension": "state",
        "expected_probe_kinds": ["log_search", "db_profile"],
        "expected_retraction_hypothesis": "h-runtime",
    }
    final = {
        "report": {
            "valid": True,
            "conclusion": {"dimension": "state", "status": "confirmed"},
            "retractions": [{"hypothesis_id": "h-runtime"}],
        }
    }
    probes = [{"kind": "log_search"}, {"kind": "db_profile"}]

    full_score, checks = benchmark._score(oracle, final, probes)
    weak_score, weak_checks = benchmark._score(oracle, final, [{"kind": "repo_state"}])

    assert full_score == 100
    assert all(checks.values())
    assert weak_score == 80
    assert weak_checks["probe_coverage"] is False


def test_runtime_benchmark_shadow_verdict_requires_every_check_in_every_case():
    passing = {"checks": {"dimension": True, "confirmed": True}}
    one_failure = {"checks": {"dimension": False, "confirmed": True}}

    assert benchmark._verdict([passing, passing]) == "shadow_ready"
    assert benchmark._verdict([passing, one_failure]) == "needs_improvement"
    assert benchmark._verdict([]) == "needs_improvement"


def test_runtime_benchmark_model_path_has_no_cloud_client_and_delays_oracle_read():
    source = inspect.getsource(benchmark)
    run_source = inspect.getsource(benchmark.run)

    assert "openai_client" not in source
    assert "gateway_chat" not in source
    assert '"premium_calls": 0' in source
    assert run_source.index("execute_safe_probes") < run_source.index("Oracle access begins")
