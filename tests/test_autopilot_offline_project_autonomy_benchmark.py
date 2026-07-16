from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "autopilot_offline_project_autonomy_benchmark.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "autopilot_offline_project_autonomy_benchmark",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_offline_benchmark_policy_rejects_premium_dependency():
    benchmark = _load_module()

    check = benchmark._policy_check()

    assert check.passed is True
    assert "premium_models_required=false" in check.evidence
    assert "frontier_default=false" in check.evidence


def test_offline_benchmark_reports_failed_local_scenario(monkeypatch):
    benchmark = _load_module()
    monkeypatch.setattr(
        benchmark,
        "_run_offline_scenario",
        lambda: (_ for _ in ()).throw(AssertionError("local editor failed")),
    )

    check = benchmark._offline_scenario_check()

    assert check.passed is False
    assert "local editor failed" in check.evidence
