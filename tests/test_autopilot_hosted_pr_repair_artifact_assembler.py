from __future__ import annotations

import json

from scripts import autopilot_hosted_pr_repair_artifact_assembler as assembler
from scripts import autopilot_hosted_pr_repair_artifact_benchmark as hosted


def test_artifact_assembler_hashes_transcripts_and_writes_valid_inventory(tmp_path):
    seed_dir = hosted._valid_inventory_dir(tmp_path / "seed")
    seed_inventory = json.loads((seed_dir / "inventory.json").read_text(encoding="utf-8"))
    artifact = seed_inventory["artifacts"][0]
    review_path = seed_dir / artifact["review_thread_transcript"]["path"]
    review_path.write_text(review_path.read_text(encoding="utf-8"), encoding="utf-8-sig")
    receipt_path = seed_dir / "post_repair_check_receipt.json"
    receipt_path.write_text(
        json.dumps(artifact["post_repair_check_receipt"], indent=2, sort_keys=True) + "\n",
        encoding="utf-8-sig",
    )
    source_manifest = seed_dir / "source_manifest.json"
    source_manifest.write_text(
        json.dumps(
            {
                "schema": assembler.SOURCE_MANIFEST_SCHEMA_VERSION,
                "pr_url": artifact["pr_url"],
                "branch": artifact["branch"],
                "source_run_id": artifact["source_run_id"],
                "repair_report": artifact["repair_report"],
                "review_thread_id": artifact["review_thread_id"],
                "line_thread": artifact["line_thread"],
                "repaired_head_sha": artifact["repaired_head_sha"],
                "post_repair_head_sha": artifact["post_repair_head_sha"],
                "current_head_sha_observed": artifact["current_head_sha_observed"],
                "hosted_run_id": artifact["hosted_run_id"],
                "current_hosted_green_run_observed": artifact[
                    "current_hosted_green_run_observed"
                ],
                "remote_publication": artifact["remote_publication"],
                "review_thread_transcript_file": artifact["review_thread_transcript"]["path"],
                "publication_transcript_file": artifact["publication_transcript"]["path"],
                "post_repair_check_receipt_file": receipt_path.name,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "assembled"

    inventory, summary, inventory_path = assembler.build_inventory_from_source_manifest(
        source_manifest,
        output_dir=output_dir,
        write=True,
    )

    assert inventory_path == output_dir / "inventory.json"
    assert inventory_path.is_file()
    assert (output_dir / "review_thread_transcript.jsonl").is_file()
    assert (output_dir / "publication_transcript.jsonl").is_file()
    assert (output_dir / "post_repair_check_receipt.json").is_file()
    assert inventory["evidence_mode"] == hosted.REAL_INVENTORY_EVIDENCE_MODE
    assert summary["validated_inventory"] is True
    assert summary["promotion_eligible"] is True
    reloaded = json.loads(inventory_path.read_text(encoding="utf-8"))
    assert hosted.validate_inventory(reloaded, base_dir=output_dir)["promotion_eligible"] is True


def test_artifact_assembler_requires_check_receipt_file(tmp_path):
    seed_dir = hosted._valid_inventory_dir(tmp_path / "seed")
    seed_inventory = json.loads((seed_dir / "inventory.json").read_text(encoding="utf-8"))
    artifact = seed_inventory["artifacts"][0]
    source_manifest = seed_dir / "source_manifest.json"
    source_manifest.write_text(
        json.dumps(
            {
                "schema": assembler.SOURCE_MANIFEST_SCHEMA_VERSION,
                "pr_url": artifact["pr_url"],
                "branch": artifact["branch"],
                "source_run_id": artifact["source_run_id"],
                "repair_report": artifact["repair_report"],
                "review_thread_id": artifact["review_thread_id"],
                "line_thread": artifact["line_thread"],
                "repaired_head_sha": artifact["repaired_head_sha"],
                "post_repair_head_sha": artifact["post_repair_head_sha"],
                "current_head_sha_observed": artifact["current_head_sha_observed"],
                "hosted_run_id": artifact["hosted_run_id"],
                "current_hosted_green_run_observed": artifact[
                    "current_hosted_green_run_observed"
                ],
                "remote_publication": artifact["remote_publication"],
                "review_thread_transcript_file": artifact["review_thread_transcript"]["path"],
                "publication_transcript_file": artifact["publication_transcript"]["path"],
                "post_repair_check_receipt_file": "missing-receipt.json",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    try:
        assembler.build_inventory_from_source_manifest(
            source_manifest,
            output_dir=tmp_path / "assembled",
            write=True,
        )
    except assembler.HostedPrRepairArtifactAssemblerError as exc:
        assert "evidence file does not exist" in str(exc)
        assert "missing-receipt.json" in str(exc)
    else:
        raise AssertionError("missing receipt file should block hosted PR artifact assembly")
