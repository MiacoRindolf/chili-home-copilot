from __future__ import annotations

from scripts import autopilot_hosted_pr_repair_collection_packet as packet


def test_collection_packet_uses_candidate_report_and_safe_commands(tmp_path):
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
    output = tmp_path / "HOSTED_PR_REPAIR_COLLECTION_PACKET.md"

    markdown, summary, output_path = packet.build_collection_packet(
        candidate_report=report,
        output_path=output,
        write=True,
    )

    assert output_path == output
    assert output.read_text(encoding="utf-8") == markdown
    assert summary["status"] == "ready"
    assert summary["pr_url"].endswith("/pull/282")
    assert "review_thread_transcript.jsonl" in summary["required_files"]
    assert "post_repair_check_receipt.json" in summary["required_files"]
    assert "autopilot_hosted_pr_repair_evidence_collector.py" in summary["collection_command"]
    assert "autopilot_hosted_pr_repair_artifact_assembler.py" in summary["artifact_assembler_command"]
    assert "autopilot_hosted_pr_repair_artifact_benchmark.py" in summary["validation_command"]
    assert "no git/PR mutation" in summary["permission_boundary"]
    assert "26879809423" in markdown
    assert "review_thread_transcript.jsonl" in markdown
    assert "post_repair_check_receipt.json" in markdown
