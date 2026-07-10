from __future__ import annotations

import json

from scripts import autopilot_hosted_pr_repair_artifact_benchmark as hosted


def test_hosted_pr_repair_artifact_cli_real_inventory_writes_promotion_ready_scorecard(tmp_path):
    artifact_dir = hosted._valid_inventory_dir(tmp_path / "artifact")
    inventory, base_dir = hosted.load_inventory(artifact_dir)
    output = tmp_path / "HOSTED_PR_REPAIR_ARTIFACT_BENCHMARK.md"

    results, markdown, output_path, summary = hosted.run_hosted_pr_repair_validation(
        inventory,
        base_dir=base_dir,
        output_path=output,
        write=True,
    )

    assert output_path == output
    assert output.read_text(encoding="utf-8") == markdown
    assert hosted.benchmark_status(results) == "passed"
    assert summary["validated_inventory"] is True
    assert summary["promotion_eligible"] is True
    assert summary["artifacts"] == 1
    assert "- Status: passed" in markdown
    assert "- Evidence mode: real_inventory" in markdown
    assert "- Checks: 18" in markdown
    assert "- Promotion eligible: true" in markdown


def test_hosted_pr_repair_artifact_accepts_hosted_ci_failure_shape(tmp_path):
    artifact_dir = hosted._valid_inventory_dir(tmp_path / "artifact")
    inventory = json.loads((artifact_dir / "inventory.json").read_text(encoding="utf-8"))
    artifact = inventory["artifacts"][0]

    assert artifact["failure_context"]["kind"] == "hosted_ci_failure"
    summary = hosted.validate_inventory(inventory, base_dir=artifact_dir)

    assert summary["validated_inventory"] is True
    assert summary["promotion_eligible"] is True
    assert summary["prs"] == ["https://github.com/MiacoRindolf/chili-home-copilot/pull/282"]
