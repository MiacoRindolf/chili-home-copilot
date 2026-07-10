from __future__ import annotations

import json

from scripts import autopilot_hosted_pr_repair_evidence_collector as collector


def test_evidence_collector_writes_manifest_template_and_safe_readme(tmp_path):
    report = tmp_path / "PR_282_CI_REPAIR.md"
    report.write_text(
        "\n".join(
            [
                "# PR 282 CI Repair Evidence",
                "",
                "- PR: https://github.com/MiacoRindolf/chili-home-copilot/pull/282",
                "- Branch: codex/stock-momentum-context-gate",
                "- Current head SHA observed: 6160d0f82d749fc04d0f74ea7030d2fd482b3e6d",
                "- Current hosted green run observed: 26879809423",
            ]
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "evidence"

    readme, summary, target_dir = collector.collect_evidence_skeleton(
        candidate_report=report,
        output_dir=output_dir,
        write=True,
    )

    template = json.loads((target_dir / "source_manifest.template.json").read_text(encoding="utf-8"))
    assert target_dir == output_dir
    assert (target_dir / "README.md").read_text(encoding="utf-8") == readme
    assert summary["status"] == "ready"
    assert summary["pr_url"].endswith("/pull/282")
    assert template["pr_url"].endswith("/pull/282")
    assert template["current_hosted_green_run_observed"] == "26879809423"
    assert template["post_repair_check_receipt_file"] == "post_repair_check_receipt.json"
    assert "post_repair_check_receipt" not in template
    assert "review_thread_transcript.jsonl" in summary["required_files"]
    assert "post_repair_check_receipt.json" in summary["required_files"]
    assert "autopilot_hosted_pr_repair_artifact_assembler.py" in summary["artifact_assembler_command"]
    assert "autopilot_hosted_pr_repair_artifact_benchmark.py" in summary["validation_command"]
    assert "no git/PR mutation" in summary["permission_boundary"]
