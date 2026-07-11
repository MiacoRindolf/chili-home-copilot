from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts import autopilot_realworld_diagnostic_benchmark as benchmark


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "project_autonomy_diagnostics"


def test_manifest_uses_fable5_and_keeps_oracles_separate():
    manifest = json.loads((FIXTURE_ROOT / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["reference_model"] == "claude-fable-5"
    assert len(manifest["cases"]) == 7
    assert {item["split"] for item in manifest["cases"]} == {"calibration", "holdout"}
    for item in manifest["cases"]:
        case = json.loads((FIXTURE_ROOT / item["case"]).read_text(encoding="utf-8"))
        oracle = json.loads((FIXTURE_ROOT / item["oracle"]).read_text(encoding="utf-8"))
        assert "expected_dimensions" not in case
        assert oracle["case_id"] == case["case_id"]


def test_heuristic_benchmark_run_is_local_only_and_never_claims_fable_parity(tmp_path):
    args = argparse.Namespace(
        fixture_root=str(FIXTURE_ROOT),
        model="qwen2.5-coder:7b",
        case=["hold-104"],
        timeout=1.0,
        num_predict=100,
        num_ctx=2048,
        keep_alive="1m",
        stages="judge",
        heuristic_only=True,
        report=str(tmp_path / "report.md"),
        results_json=str(tmp_path / "results.json"),
        json=False,
    )

    result = benchmark.run(args)

    assert result["premium_calls"] == 0
    assert result["fable5_head_to_head_run"] is False
    assert result["fable5_parity_claim"] is False
    assert result["cases"][0]["score_detail"]["checks"]["safety"] is True
    assert "Fable 5 parity claim: **No**" in (tmp_path / "report.md").read_text(encoding="utf-8")
